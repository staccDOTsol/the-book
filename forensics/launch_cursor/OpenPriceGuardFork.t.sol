// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {OpenPriceGuard, IOpenPricePonsFactory, IOpenPriceStateView} from "./OpenPriceGuard.sol";

/// @dev Q has code and a v4 pool key, but is deliberately not deployed on Robinhood.
contract OpenPriceForkSyntheticQuote {
    string public constant name = "Fork Synthetic Q";
    string public constant symbol = "Q";
    uint8 public constant decimals = 18;
}

/// @dev Only Q/ETH is synthetic. Pons pool reads go to the chain's StateView.
contract OpenPriceForkStateView is IOpenPriceStateView {
    IOpenPriceStateView public immutable realStateView;
    bytes32 public immutable quoteEthPoolId;
    uint160 public immutable quoteEthSqrtPriceX96;
    uint24 public immutable quoteEthFeePips;

    constructor(
        address realStateView_,
        bytes32 quoteEthPoolId_,
        uint160 quoteEthSqrtPriceX96_,
        uint24 quoteEthFeePips_
    ) {
        realStateView = IOpenPriceStateView(realStateView_);
        quoteEthPoolId = quoteEthPoolId_;
        quoteEthSqrtPriceX96 = quoteEthSqrtPriceX96_;
        quoteEthFeePips = quoteEthFeePips_;
    }

    function poolManager() external view override returns (address) {
        return realStateView.poolManager();
    }

    function getSlot0(bytes32 poolId)
        external
        view
        override
        returns (uint160 sqrtPriceX96, int24 tick, uint24 protocolFee, uint24 lpFee)
    {
        if (poolId == quoteEthPoolId) return (quoteEthSqrtPriceX96, 0, 0, quoteEthFeePips);
        return realStateView.getSlot0(poolId);
    }

    function getLiquidity(bytes32 poolId) external view override returns (uint128) {
        if (poolId == quoteEthPoolId) return 1 ether;
        return realStateView.getLiquidity(poolId);
    }
}

/// @notice Runs on a Robinhood mainnet fork. All contracts deployed here are local to the test EVM.
contract OpenPriceGuardForkTest {
    address private constant PONS_FACTORY = 0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e;
    address private constant PONS_HOOK = 0xE5e702641Ea86F4ae6cC3cDaeD2B886f976Be044;
    address private constant STATE_VIEW = 0xF3334192D15450CdD385c8B70e03f9A6bD9E673b;
    address private constant LAUNCH_TOKEN = 0x3AA0cE359da22665eA478817A2F8C53Dff802DEA;
    uint24 private constant QUOTE_ETH_FEE_PIPS = 2_500;
    int24 private constant QUOTE_ETH_TICK_SPACING = 25;
    uint160 private constant SYNTHETIC_QUOTE_ETH_SQRT_PRICE_X96 = uint160(1 << 96);
    uint16 private constant MAX_DEVIATION_BPS = 1_000;

    function _guard() private returns (OpenPriceGuard guard, OpenPriceForkStateView wrapped) {
        require(block.chainid == 4663, "run with Robinhood fork");
        OpenPriceForkSyntheticQuote q = new OpenPriceForkSyntheticQuote();
        bytes32 quoteEthPoolId =
            keccak256(abi.encode(address(0), address(q), QUOTE_ETH_FEE_PIPS, QUOTE_ETH_TICK_SPACING, address(0)));
        wrapped = new OpenPriceForkStateView(
            STATE_VIEW, quoteEthPoolId, SYNTHETIC_QUOTE_ETH_SQRT_PRICE_X96, QUOTE_ETH_FEE_PIPS
        );
        guard = new OpenPriceGuard(
            PONS_FACTORY, address(wrapped), address(q), QUOTE_ETH_FEE_PIPS, QUOTE_ETH_TICK_SPACING, MAX_DEVIATION_BPS
        );
        require(guard.ponsHook() == PONS_HOOK, "wrong real Pons hook");
        require(guard.quoteEthPoolId() == quoteEthPoolId, "wrong synthetic pool key");
    }

    function testReferenceUsesRealGraduatedPonsPool() external {
        (OpenPriceGuard guard, OpenPriceForkStateView wrapped) = _guard();
        IOpenPricePonsFactory.LaunchedToken memory launch =
            IOpenPricePonsFactory(PONS_FACTORY).getLaunchedToken(LAUNCH_TOKEN);
        require(launch.exists && launch.token == LAUNCH_TOKEN, "missing real launch");
        require(launch.phase == 2 && launch.pairToken == address(0), "not ETH phase 2");

        bytes32 ponsPoolId =
            keccak256(abi.encode(address(0), LAUNCH_TOKEN, launch.poolFee, launch.tickSpacing, PONS_HOOK));
        IOpenPriceStateView real = IOpenPriceStateView(STATE_VIEW);
        (uint160 realSqrt, int24 realTick, uint24 realProtocolFee, uint24 realLpFee) = real.getSlot0(ponsPoolId);
        (uint160 wrappedSqrt, int24 wrappedTick, uint24 wrappedProtocolFee, uint24 wrappedLpFee) =
            wrapped.getSlot0(ponsPoolId);
        require(realSqrt != 0 && wrappedSqrt == realSqrt, "Pons price not forwarded");
        require(
            wrappedTick == realTick && wrappedProtocolFee == realProtocolFee && wrappedLpFee == realLpFee,
            "Pons slot0 not forwarded"
        );
        require(real.getLiquidity(ponsPoolId) > 0, "real Pons pool has no liquidity");
        require(wrapped.getLiquidity(ponsPoolId) == real.getLiquidity(ponsPoolId), "Pons liquidity not forwarded");

        uint160 refSqrt = guard.referenceSqrtPriceX96(LAUNCH_TOKEN);
        require(refSqrt != 0, "no cross-price reference");
        require(guard.validate(LAUNCH_TOKEN, refSqrt) == refSqrt, "reference rejected");
        require(guard.isWithinBound(LAUNCH_TOKEN, refSqrt), "reference outside bound");
    }

    function testValidateRejectsPriceAboveMaxDeviation() external {
        (OpenPriceGuard guard,) = _guard();
        uint160 refSqrt = guard.referenceSqrtPriceX96(LAUNCH_TOKEN);
        // A 20% increase in sqrt price makes the proposed raw price 44% higher.
        uint160 proposed = uint160(uint256(refSqrt) * 120 / 100);
        require(proposed > refSqrt, "proposed price did not rise");
        require(!guard.isWithinBound(LAUNCH_TOKEN, proposed), "excess deviation accepted");

        (bool ok, bytes memory revertData) =
            address(guard).staticcall(abi.encodeCall(OpenPriceGuard.validate, (LAUNCH_TOKEN, proposed)));
        require(!ok, "validate accepted excess deviation");
        require(
            keccak256(revertData)
                == keccak256(abi.encodeWithSelector(OpenPriceGuard.PriceOutsideBound.selector, proposed, refSqrt)),
            "wrong rejection reason"
        );
    }
}
