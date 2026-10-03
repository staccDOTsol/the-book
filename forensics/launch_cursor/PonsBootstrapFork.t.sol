// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {HooklessLPExecutor, IHooklessPositionManager} from "./HooklessLPExecutor.sol";
import {PositionInspector} from "./PositionInspector.sol";
import {LaunchCursorToken} from "./LaunchCursorToken.sol";
import {PonsBootstrap, IBootstrapLauncher, IBootstrapStateView, IBootstrapInstantStrategy}
    from "./PonsBootstrap.s.sol";

interface IBootstrapForkVm {
    function deal(address who, uint256 newBalance) external;
    function skip(bool shouldSkip) external;
    function setEnv(string calldata name, string calldata value) external;
    function toString(address value) external returns (string memory);
    function toString(uint256 value) external returns (string memory);
}

/// @notice Real Robinhood fork exercise of Uniswap's current existing-token
/// Instant Launch path. Forge forks state locally; no transaction is sent.
contract PonsBootstrapForkTest {
    event log_named_uint(string key, uint256 val);
    IBootstrapForkVm private constant vm =
        IBootstrapForkVm(address(uint160(uint256(keccak256("hevm cheat code")))));
    uint256 private constant SUPPLY = 1_000_000_000 ether;
    address private constant POOL_MANAGER = 0x8366a39CC670B4001A1121B8F6A443A643e40951;
    address private constant POSITION_MANAGER = 0x58daec3116aae6D93017bAAea7749052E8a04fA7;
    address private constant STATE_VIEW = 0xF3334192D15450CdD385c8B70e03f9A6bD9E673b;
    address private constant PERMIT2 = 0x000000000022D473030F116dDEE9F6B43aC78BA3;
    address private constant PONS_FACTORY = 0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e;
    address private constant LAUNCHER = 0x0000FffFBE8efE702c8703aE3477FF5dE3d319C0;
    address private constant FEES_ON = 0x7c48DDe3B447381F4d986334679b3Afc7F2D35C2;
    address private constant FEES_OFF = 0xC9566675b1Ea42861546f3c5B74Ace2c79c49572;

    function testExistingQBothCurrentInstantLaunchVariants() external {
        if (block.chainid != 4663) {
            vm.skip(true);
            return;
        }
        _exercise(FEES_ON, "Fork Q On", "FQON", address(0xBEEF));
        _exercise(FEES_OFF, "Fork Q Off", "FQOFF", address(0xBEEF));
    }

    function _exercise(address strategy, string memory name, string memory symbol, address beneficiary) private {
        require(block.chainid == 4663, "Robinhood fork required");
        require(IBootstrapInstantStrategy(strategy).launcher() == LAUNCHER, "wrong launcher");
        require(IBootstrapLauncher(LAUNCHER).permit2() == PERMIT2, "wrong Permit2");

        HooklessLPExecutor executor = new HooklessLPExecutor(
            POOL_MANAGER, POSITION_MANAGER, STATE_VIEW, PERMIT2
        );
        PositionInspector inspector = new PositionInspector(address(executor));
        address[] memory endpoints = new address[](4);
        endpoints[0] = PERMIT2;
        endpoints[1] = LAUNCHER;
        endpoints[2] = strategy;
        endpoints[3] = POSITION_MANAGER;
        LaunchCursorToken q = new LaunchCursorToken(
            LaunchCursorToken.Metadata(name, symbol, "Fork launch quote", "ipfs://fork-launch-q"), SUPPLY,
            PONS_FACTORY, address(executor), address(inspector),
            30, 3_000_000, 1 gwei, endpoints
        );
        executor.bindController(address(q));
        inspector.bindCursor(address(q));
        q.setAutomatic(false);

        require(q.totalSupply() == SUPPLY && q.balanceOf(address(this)) == SUPPLY, "wrong initial Q supply");
        uint256 nextNft = _launchWithPreflight(executor, inspector, q, strategy, beneficiary);

        bytes32 poolId = keccak256(abi.encode(address(0), address(q), uint24(2_500), int24(25), address(0)));
        (uint160 sqrtPriceX96,,, uint24 liveFee) = IBootstrapStateView(STATE_VIEW).getSlot0(poolId);
        require(sqrtPriceX96 != 0 && liveFee == 2_500, "Q/ETH launch pool invalid");
        require(q.balanceOf(LAUNCHER) == 0 && q.balanceOf(strategy) == 0, "launch Q custody remains");
        require(q.totalSupply() <= SUPPLY && q.balanceOf(address(this)) == 0, "launch did not consume Q");
        require(IHooklessPositionManager(POSITION_MANAGER).ownerOf(nextNft) ==
            IBootstrapInstantStrategy(strategy).feeSplitter(), "launch NFT not locked in splitter");
        require(IHooklessPositionManager(POSITION_MANAGER).getPositionLiquidity(nextNft) > 0, "launch NFT has no LP");

        // The launch opens exactly at the upper edge of its single-sided LP,
        // so active pool liquidity can read as zero until the first ETH buy.
        // The operational price guard requires positive active liquidity.
        PonsBootstrap bootstrap = new PonsBootstrap();
        _fundVault(bootstrap, executor, q, poolId);

        vm.setEnv("PONS_DEVELOPER", vm.toString(address(0xD00D)));
        vm.setEnv("PONS_PRICE_CONFIGURATOR", vm.toString(address(0xA11CE)));
        vm.setEnv("PONS_EXIT_CONFIGURATOR", vm.toString(address(0xE417)));
        vm.setEnv("PONS_SPOT_MAX_DEVIATION_BPS", "1000");
        (address spot, address router) = bootstrap.deployAfterLaunch();
        require(spot.code.length != 0 && executor.settlementRouter() == router,
            "post-launch bindings wrong");
        require(!q.automaticEnabled(), "cursor enabled before activation");
        require(q.priceConfigurator() == address(0xA11CE) &&
            q.exitConfigurator() == address(0xE417), "keeper roles were not isolated");
        bootstrap.activate();
        require(q.automaticEnabled(), "activation failed");
    }

    function _fundVault(PonsBootstrap bootstrap, HooklessLPExecutor executor, LaunchCursorToken q, bytes32 poolId)
        private
    {
        vm.deal(address(this), 1 ether);
        address buyer = bootstrap.deployVaultBuyer();
        vm.setEnv("PONS_VAULT_BUY_ADAPTER_ADDRESS", vm.toString(buyer));
        vm.setEnv("PONS_VAULT_BUY_ETH_WEI", vm.toString(uint256(0.01 ether)));
        vm.setEnv("PONS_VAULT_BUY_SLIPPAGE_BPS", "1000");
        vm.setEnv("PONS_VAULT_BUY_TTL_SECONDS", "60");
        (address plannedBuyer, uint256 ethIn, uint256 quoteQ, uint256 minQOut,, bytes memory buyCalldata) =
            bootstrap.preflightVaultBuy();
        require(plannedBuyer == buyer && ethIn == 0.01 ether && quoteQ >= minQOut && minQOut > 0,
            "bounded vault buy plan invalid");
        emit log_named_uint("Q from first 0.01 ETH buy", quoteQ);
        emit log_named_uint("Q first-buy average ETH wei per whole Q", ethIn * 1 ether / quoteQ);
        (bool bought,) = buyer.call{value: ethIn}(buyCalldata);
        require(bought && q.balanceOf(address(executor)) > 0, "first vault Q buy failed");
        require(IBootstrapStateView(STATE_VIEW).getLiquidity(poolId) > 0, "Q/ETH not active after buy");
        (uint160 afterBuySqrt,,,) = IBootstrapStateView(STATE_VIEW).getSlot0(poolId);
        emit log_named_uint("Q/ETH sqrtPriceX96 after first buy", afterBuySqrt);
    }

    function _launchWithPreflight(
        HooklessLPExecutor executor, PositionInspector inspector, LaunchCursorToken q,
        address strategy, address beneficiary
    ) private returns (uint256 nextNft) {
        vm.setEnv("PONS_DEPLOYER", vm.toString(address(this)));
        vm.setEnv("PONS_INSTANT_STRATEGY", vm.toString(strategy));
        vm.setEnv("PONS_EXECUTOR_ADDRESS", vm.toString(address(executor)));
        vm.setEnv("PONS_INSPECTOR_ADDRESS", vm.toString(address(inspector)));
        vm.setEnv("PONS_Q_ADDRESS", vm.toString(address(q)));
        vm.setEnv("PONS_FEE_BENEFICIARY", vm.toString(beneficiary));
        vm.setEnv("PONS_PERMIT2_EXPIRATION", vm.toString(block.timestamp + 1 hours));
        (address plannedQ, address plannedLauncher, bytes memory qApprove,
            bytes memory permit2Approve, bytes memory atomicLaunch) = new PonsBootstrap().preflightLaunch();
        require(plannedQ == address(q) && plannedLauncher == LAUNCHER, "wrong launch targets");
        nextNft = IHooklessPositionManager(POSITION_MANAGER).nextTokenId();
        (bool qApproved,) = address(q).call(qApprove);
        require(qApproved, "Q approval failed");
        (bool permit2Approved,) = PERMIT2.call(permit2Approve);
        require(permit2Approved, "Permit2 approval failed");
        (bool launched,) = LAUNCHER.call(atomicLaunch);
        require(launched, "atomic launcher call failed");
    }
}
