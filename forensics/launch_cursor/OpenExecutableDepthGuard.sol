// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {OpenPriceFullMath, IOpenPricePonsFactory} from "./OpenPriceGuard.sol";
import {HooklessTickMath} from "./HooklessLPExecutor.sol";

interface IDepthPriceGuard {
    function ponsFactory() external view returns (address);
    function stateView() external view returns (address);
    function quoteToken() external view returns (address);
    function ponsHook() external view returns (address);
    function quoteEthPoolId() external view returns (bytes32);
    function quoteEthFeePips() external view returns (uint24);
    function quoteEthTickSpacing() external view returns (int24);
}

interface IDepthStateView {
    function poolManager() external view returns (address);
}

interface IDepthSettlementRouter {
    function source() external view returns (address);
    function quoteToken() external view returns (address);
    function factory() external view returns (address);
    function activeSale() external view returns (address);
}

interface IDepthActiveSale {
    function previewNativeActiveSale(address token, uint256 tokensIn)
        external view returns (address curve, uint256 ethOut);
}

interface IDepthV4Quoter {
    function poolManager() external view returns (address);

    struct PoolKey {
        address currency0;
        address currency1;
        uint24 fee;
        int24 tickSpacing;
        address hooks;
    }

    struct QuoteExactSingleParams {
        PoolKey poolKey;
        bool zeroForOne;
        uint128 exactAmount;
        bytes hookData;
    }

    // v4 Quoter swaps inside PoolManager.unlock, so this is intentionally non-view.
    function quoteExactInputSingle(QuoteExactSingleParams calldata params)
        external returns (uint256 amountOut, uint256 gasEstimate);
}

/// @notice Requotes the entire X inventory an X/Q LP could acquire before
/// opening, at current executable Pons and Q/ETH depth. A saved open plan
/// cannot survive a collapse in sale depth merely because marginal spot stays
/// unchanged. The owner fixes the safety haircut when deploying this guard.
contract OpenExecutableDepthGuard {
    uint256 private constant ROBINHOOD_CHAIN_ID = 4663;
    address private constant ROBINHOOD_V4_QUOTER = 0x8Dc178eFB8111BB0973Dd9d722ebeFF267c98F94;
    uint256 private constant Q96 = 1 << 96;
    uint256 private constant Q128 = 1 << 128;
    uint256 private constant BPS = 10_000;

    address public immutable source;
    address public immutable priceGuard;
    address public immutable settlementRouter;
    address public immutable quoteToken;
    address public immutable ponsFactory;
    address public immutable quoter;
    uint16 public immutable safetyBps;

    error InvalidConfiguration();
    error UnsupportedLaunchPhase(uint8 phase);
    error NoExecutableDepth();
    error InsufficientExecutableDepth(uint256 safeQ, uint256 requiredQ);

    constructor(
        address source_, address priceGuard_, address settlementRouter_,
        address quoter_, uint16 safetyBps_
    ) {
        if (
            block.chainid != ROBINHOOD_CHAIN_ID || quoter_ != ROBINHOOD_V4_QUOTER ||
            source_.code.length == 0 || priceGuard_.code.length == 0 ||
            settlementRouter_.code.length == 0 || quoter_.code.length == 0 ||
            safetyBps_ == 0 || safetyBps_ >= BPS
        ) revert InvalidConfiguration();
        IDepthPriceGuard spot = IDepthPriceGuard(priceGuard_);
        IDepthSettlementRouter router = IDepthSettlementRouter(settlementRouter_);
        address q = spot.quoteToken();
        address factory = spot.ponsFactory();
        address manager = IDepthStateView(spot.stateView()).poolManager();
        if (
            q.code.length == 0 || factory.code.length == 0 || manager.code.length == 0 ||
            IDepthV4Quoter(quoter_).poolManager() != manager ||
            router.source() != source_ || router.quoteToken() != q ||
            router.factory() != factory || router.activeSale().code.length == 0 ||
            spot.quoteEthPoolId() != keccak256(abi.encode(
                address(0), q, spot.quoteEthFeePips(), spot.quoteEthTickSpacing(), address(0)
            ))
        ) revert InvalidConfiguration();
        source = source_;
        priceGuard = priceGuard_;
        settlementRouter = settlementRouter_;
        quoteToken = q;
        ponsFactory = factory;
        quoter = quoter_;
        safetyBps = safetyBps_;
    }

    function matches(address source_, address priceGuard_, address settlementRouter_)
        external view returns (bool)
    {
        return source_ == source && priceGuard_ == priceGuard && settlementRouter_ == settlementRouter;
    }

    /// @return maxX Rounded-up full-band X inventory, not a small spot sample.
    /// @return executableQ Exact-size X->ETH->Q quote before the haircut.
    /// @return safeQ Quote after the fixed safety haircut.
    /// @return requiredQ Conservative Q value of maxX at the first buy boundary.
    function validate(address token, uint128 liquidity, int24 tickLower, int24 tickUpper)
        external returns (uint256 maxX, uint256 executableQ, uint256 safeQ, uint256 requiredQ)
    {
        if (
            msg.sender != source || token == address(0) || token == quoteToken ||
            liquidity == 0 || tickLower >= tickUpper
        ) revert InvalidConfiguration();
        uint160 lower = HooklessTickMath.getSqrtPriceAtTick(tickLower);
        uint160 upper = HooklessTickMath.getSqrtPriceAtTick(tickUpper);
        bool quoteIs0 = uint160(quoteToken) < uint160(token);
        maxX = quoteIs0
            ? _ceilMulDiv(liquidity, uint256(upper) - lower, Q96)
            : _ceilDiv(_ceilMulDiv(uint256(liquidity) << 96, uint256(upper) - lower, upper), lower);
        if (maxX == 0 || maxX > type(uint128).max) revert NoExecutableDepth();

        IOpenPricePonsFactory.LaunchedToken memory launch =
            IOpenPricePonsFactory(ponsFactory).getLaunchedToken(token);
        if (
            !launch.exists || launch.token != token || launch.pairToken != address(0) ||
            launch.curve.code.length == 0
        ) revert InvalidConfiguration();
        uint256 ethOut;
        if (launch.phase == 0) {
            (, ethOut) = IDepthActiveSale(IDepthSettlementRouter(settlementRouter).activeSale())
                .previewNativeActiveSale(token, maxX);
        } else if (launch.phase == 2) {
            ethOut = _quoteSingle(
                IDepthV4Quoter.PoolKey({
                    currency0: address(0), currency1: token, fee: launch.poolFee,
                    tickSpacing: launch.tickSpacing,
                    hooks: IDepthPriceGuard(priceGuard).ponsHook()
                }), false, uint128(maxX)
            );
        } else {
            revert UnsupportedLaunchPhase(launch.phase);
        }
        if (ethOut == 0 || ethOut > type(uint128).max) revert NoExecutableDepth();
        IDepthPriceGuard spot = IDepthPriceGuard(priceGuard);
        executableQ = _quoteSingle(
            IDepthV4Quoter.PoolKey({
                currency0: address(0), currency1: quoteToken,
                fee: spot.quoteEthFeePips(), tickSpacing: spot.quoteEthTickSpacing(),
                hooks: address(0)
            }), true, uint128(ethOut)
        );
        safeQ = OpenPriceFullMath.mulDiv(executableQ, BPS - safetyBps, BPS);
        if (safeQ == 0) revert NoExecutableDepth();

        // sqrt^2 / 2^64 is Q128-scaled. Q0 needs the inverse X/Q price;
        // flooring that denominator raises required Q. Q1 rounds the direct
        // Q/X price up. Both comparisons are conservative at one wei or less.
        uint160 boundary = quoteIs0 ? lower : upper;
        uint256 ratioX128 = OpenPriceFullMath.mulDiv(boundary, boundary, 1 << 64);
        requiredQ = quoteIs0
            ? _ceilMulDiv(maxX, Q128, ratioX128)
            : _ceilMulDiv(maxX, ratioX128 + 1, Q128);
        if (safeQ < requiredQ) revert InsufficientExecutableDepth(safeQ, requiredQ);
    }

    function _quoteSingle(IDepthV4Quoter.PoolKey memory key, bool zeroForOne, uint128 exactAmount)
        private returns (uint256 amountOut)
    {
        (amountOut,) = IDepthV4Quoter(quoter).quoteExactInputSingle(
            IDepthV4Quoter.QuoteExactSingleParams({
                poolKey: key, zeroForOne: zeroForOne,
                exactAmount: exactAmount, hookData: bytes("")
            })
        );
        if (amountOut == 0) revert NoExecutableDepth();
    }

    function _ceilMulDiv(uint256 a, uint256 b, uint256 denominator) private pure returns (uint256 result) {
        result = OpenPriceFullMath.mulDiv(a, b, denominator);
        if (mulmod(a, b, denominator) != 0) ++result;
    }

    function _ceilDiv(uint256 a, uint256 b) private pure returns (uint256) {
        return a / b + (a % b == 0 ? 0 : 1);
    }
}
