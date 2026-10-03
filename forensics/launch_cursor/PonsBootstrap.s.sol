// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {HooklessLPExecutor} from "./HooklessLPExecutor.sol";
import {PositionInspector} from "./PositionInspector.sol";
import {LaunchCursorToken} from "./LaunchCursorToken.sol";
import {OpenPriceGuard} from "./OpenPriceGuard.sol";
import {ExitSettlementRouter} from "./ExitSettlementRouter.sol";
import {HooklessQuoteBuyAdapter} from "./HooklessQuoteBuyAdapter.sol";

// This script deliberately has no key-loading method. Forge supplies the
// signer for a future broadcast; the script only reads public configuration.
interface IBootstrapVm {
    function envAddress(string calldata name) external returns (address);
    function envString(string calldata name) external returns (string memory);
    function envUint(string calldata name) external returns (uint256);
    function startBroadcast(address signer) external;
    function stopBroadcast() external;
}

interface IBootstrapPoolLinked {
    function poolManager() external view returns (address);
}

interface IBootstrapPonsFactory is IBootstrapPoolLinked {
    function memeHook() external view returns (address);
}

interface IBootstrapPonsHook is IBootstrapPoolLinked {
    function factory() external view returns (address);
}

interface IBootstrapInstantStrategy {
    function launcher() external view returns (address);
    function poolManager() external view returns (address);
    function positionManager() external view returns (address);
    function feeSplitter() external view returns (address);
    function beneficiaryVault() external view returns (address);
    function TOTAL_SUPPLY() external view returns (uint256);
    function LP_FEE() external view returns (uint24);
    function TICK_SPACING() external view returns (int24);
    function initialTick() external view returns (int24);
}

interface IBootstrapLauncher {
    struct Distribution {
        address strategy;
        uint128 amount;
        bytes configData;
    }

    function permit2() external view returns (address);
    function depositToken(address token, uint160 amount) external payable;
    function distributeToken(address token, Distribution calldata distribution, bytes32 salt) external payable;
    function multicall(bytes[] calldata data) external payable returns (bytes[] memory);
}

interface IBootstrapPermit2 {
    function approve(address token, address spender, uint160 amount, uint48 expiration) external;
}

interface IBootstrapStateView is IBootstrapPoolLinked {
    function getSlot0(bytes32 poolId)
        external view returns (uint160 sqrtPriceX96, int24 tick, uint24 protocolFee, uint24 lpFee);
    function getLiquidity(bytes32 poolId) external view returns (uint128);
}

interface IBootstrapQuoter is IBootstrapPoolLinked {
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

    function quoteExactInputSingle(QuoteExactSingleParams calldata params)
        external returns (uint256 amountOut, uint256 gasEstimate);
}

/// @notice Staged Robinhood deployment for the hookless Pons X/Q prototype.
/// @dev Each future broadcast stage consists of multiple transactions. Stop on
/// any failed or unexpected receipt and reconcile addresses before resuming.
/// Calling preflightLaunch only returns calldata; it never sends approvals or
/// calls LiquidityLauncher. No entry point reads a private key.
contract PonsBootstrap {
    IBootstrapVm private constant vm =
        IBootstrapVm(address(uint160(uint256(keccak256("hevm cheat code")))));

    uint256 private constant CHAIN_ID = 4663;
    uint256 private constant SUPPLY = 1_000_000_000 ether;
    uint24 private constant Q_ETH_FEE = 2_500;
    int24 private constant Q_ETH_SPACING = 25;
    uint160 private constant MIN_SQRT_PRICE = 4_295_128_739;
    uint160 private constant MAX_SQRT_PRICE = 1_461_446_703_485_210_103_287_273_052_203_988_822_378_723_970_342;

    address private constant POOL_MANAGER = 0x8366a39CC670B4001A1121B8F6A443A643e40951;
    address private constant POSITION_MANAGER = 0x58daec3116aae6D93017bAAea7749052E8a04fA7;
    address private constant STATE_VIEW = 0xF3334192D15450CdD385c8B70e03f9A6bD9E673b;
    address private constant QUOTER = 0x8Dc178eFB8111BB0973Dd9d722ebeFF267c98F94;
    address private constant PERMIT2 = 0x000000000022D473030F116dDEE9F6B43aC78BA3;
    address private constant PONS_FACTORY = 0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e;
    address private constant LAUNCHER = 0x0000FffFBE8efE702c8703aE3477FF5dE3d319C0;
    address private constant INSTANT_FEES_ON = 0x7c48DDe3B447381F4d986334679b3Afc7F2D35C2;
    address private constant INSTANT_FEES_OFF = 0xC9566675b1Ea42861546f3c5B74Ace2c79c49572;
    address private constant FEES_ON_SPLITTER = 0x9411fa7F956f64aa7981AA27cB3bC6eC0415449C;
    address private constant FEES_OFF_SPLITTER = 0x882Ae5e2095435A62Fd1BBDEfcb637f5CeAFc0ee;
    address private constant BENEFICIARY_VAULT = 0x26d2F7AcB07707034406a0dC458351Bb63C02553;
    address private constant WETH = 0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73;
    address private constant WIZARD_FANOUT = 0x1b88A6c6516FD2918905186F21Bb9F5CaA1a15c8;
    bytes32 private constant WIZARD_FANOUT_CODEHASH =
        0x384c9220050083b0efd1cac6ac47ea6901e68a0a10a9ff1ad3a06ddade6d21ae;
    string private constant Q_LABEL = unicode"🧙‍♂️";
    string private constant Q_IMAGE_URI =
        "https://raw.githubusercontent.com/staccDOTsol/the-book/450d558b169408de1483c0540faa1aae72889a57/assets/wizard-token.png";

    error PreflightFailed(string reason);

    function deployCore() external returns (address executor, address inspector, address quoteToken) {
        address operator = vm.envAddress("PONS_DEPLOYER");
        address strategy = vm.envAddress("PONS_INSTANT_STRATEGY");
        _requireNonzero(operator, "operator");
        _verifyExternal(strategy);

        string memory name = _pinnedMetadata("PONS_Q_NAME", Q_LABEL);
        string memory symbol = _pinnedMetadata("PONS_Q_SYMBOL", Q_LABEL);
        string memory description = _pinnedMetadata("PONS_Q_DESCRIPTION", Q_LABEL);
        string memory imageURI = _pinnedMetadata("PONS_Q_IMAGE_URI", Q_IMAGE_URI);
        uint256 retry = vm.envUint("PONS_RETRY_DELAY_SECONDS");
        uint256 gasLimit = vm.envUint("PONS_TRANSFER_STEP_GAS_LIMIT");
        uint256 gasPrice = vm.envUint("PONS_HARVEST_GAS_PRICE_CEILING_WEI");
        if (retry == 0 || retry > type(uint64).max || gasLimit < 100_000 || gasLimit > 10_000_000 || gasPrice == 0) {
            revert PreflightFailed("cursor policy out of range");
        }

        // Q disables automatic work before its first launch. These endpoints
        // also suppress cursor attempts during the launcher transfer path.
        address[] memory endpoints = new address[](8);
        endpoints[0] = POOL_MANAGER;
        endpoints[1] = POSITION_MANAGER;
        endpoints[2] = PERMIT2;
        endpoints[3] = LAUNCHER;
        endpoints[4] = strategy;
        endpoints[5] = IBootstrapInstantStrategy(strategy).feeSplitter();
        endpoints[6] = PONS_FACTORY;
        endpoints[7] = QUOTER;

        vm.startBroadcast(operator);
        HooklessLPExecutor ex = new HooklessLPExecutor(POOL_MANAGER, POSITION_MANAGER, STATE_VIEW, PERMIT2);
        PositionInspector ins = new PositionInspector(address(ex));
        LaunchCursorToken q = new LaunchCursorToken(
            LaunchCursorToken.Metadata(name, symbol, description, imageURI),
            SUPPLY, PONS_FACTORY, address(ex), address(ins),
            uint64(retry), uint32(gasLimit), gasPrice, endpoints
        );
        ex.bindController(address(q));
        ins.bindCursor(address(q));
        q.setAutomatic(false);
        vm.stopBroadcast();

        _verifyCore(operator, ex, ins, q);
        if (q.automaticEnabled()) revert PreflightFailed("automatic cursor enabled during launch");
        return (address(ex), address(ins), address(q));
    }

    /// @notice Read-only launch check and exact calldata for an owner-wallet
    /// existing-token launch. The first two approvals are separate writes; the
    /// deposit and distribution are encoded in ONE atomic launcher multicall.
    function preflightLaunch()
        external returns (address quoteToken, address launcher, bytes memory qApprove,
            bytes memory permit2Approve, bytes memory atomicLaunch)
    {
        address operator = vm.envAddress("PONS_DEPLOYER");
        address strategy = vm.envAddress("PONS_INSTANT_STRATEGY");
        address beneficiary = vm.envAddress("PONS_FEE_BENEFICIARY");
        uint256 expiry = vm.envUint("PONS_PERMIT2_EXPIRATION");
        (HooklessLPExecutor ex, PositionInspector ins, LaunchCursorToken q) = _coreFromEnv();
        _verifyExternal(strategy);
        _verifyCore(operator, ex, ins, q);
        if (!q.internalEndpoint(strategy)) revert PreflightFailed("launch strategy not allowlisted in Q");
        if (q.automaticEnabled()) revert PreflightFailed("disable automatic cursor before launch");
        if (q.totalSupply() != SUPPLY || q.balanceOf(operator) != SUPPLY) {
            revert PreflightFailed("owner must hold the whole fixed Q supply");
        }
        if (q.balanceOf(LAUNCHER) != 0 || q.balanceOf(strategy) != 0) {
            revert PreflightFailed("launcher or strategy already holds Q");
        }
        if (expiry <= block.timestamp || expiry > type(uint48).max) revert PreflightFailed("Permit2 expiry invalid");
        if (beneficiary == address(0) || beneficiary == LAUNCHER) revert PreflightFailed("beneficiary invalid");
        bytes32 poolId = _quoteEthPoolId(address(q));
        (uint160 sqrtPriceX96,,,) = IBootstrapStateView(STATE_VIEW).getSlot0(poolId);
        if (sqrtPriceX96 != 0) revert PreflightFailed("Q/ETH pool already initialized");

        qApprove = abi.encodeWithSignature("approve(address,uint256)", PERMIT2, SUPPLY);
        permit2Approve = abi.encodeCall(
            IBootstrapPermit2.approve, (address(q), LAUNCHER, uint160(SUPPLY), uint48(expiry))
        );
        IBootstrapLauncher.Distribution memory distribution = IBootstrapLauncher.Distribution({
            strategy: strategy, amount: uint128(SUPPLY), configData: abi.encode(beneficiary)
        });
        bytes[] memory calls = new bytes[](2);
        calls[0] = abi.encodeCall(IBootstrapLauncher.depositToken, (address(q), uint160(SUPPLY)));
        calls[1] = abi.encodeCall(IBootstrapLauncher.distributeToken, (address(q), distribution, bytes32(0)));
        atomicLaunch = abi.encodeCall(IBootstrapLauncher.multicall, (calls));
        return (address(q), LAUNCHER, qApprove, permit2Approve, atomicLaunch);
    }

    /// @notice Deploy an owner-only Q/ETH buyer after Q's launch. Its output
    /// recipient is later fixed by preflightVaultBuy to the executor.
    function deployVaultBuyer() external returns (address buyer) {
        address operator = vm.envAddress("PONS_DEPLOYER");
        address strategy = vm.envAddress("PONS_INSTANT_STRATEGY");
        (HooklessLPExecutor ex, PositionInspector ins, LaunchCursorToken q) = _coreFromEnv();
        _verifyExternal(strategy);
        _verifyCore(operator, ex, ins, q);
        if (!q.internalEndpoint(strategy)) revert PreflightFailed("launch strategy not allowlisted in Q");
        _verifyInitializedQPool(strategy, q);
        vm.startBroadcast(operator);
        HooklessQuoteBuyAdapter adapter = new HooklessQuoteBuyAdapter(
            POOL_MANAGER, address(q), Q_ETH_FEE, Q_ETH_SPACING, operator
        );
        q.setInternalEndpoint(address(adapter), true);
        vm.stopBroadcast();
        if (!q.internalEndpoint(address(adapter))) revert PreflightFailed("vault buyer not allowlisted in Q");
        return address(adapter);
    }

    /// @notice Read-only, size-aware Q buy plan. The returned buyQ calldata
    /// must be sent by the owner to the verified buyer with exactly ethIn wei;
    /// its recipient is the executor. The short deadline and nonzero minimum
    /// bound state changes between quoting and execution.
    function preflightVaultBuy()
        external returns (address buyer, uint256 ethIn, uint256 quotedQ,
            uint256 minQOut, uint64 deadline, bytes memory buyCalldata)
    {
        address operator = vm.envAddress("PONS_DEPLOYER");
        address strategy = vm.envAddress("PONS_INSTANT_STRATEGY");
        buyer = vm.envAddress("PONS_VAULT_BUY_ADAPTER_ADDRESS");
        ethIn = vm.envUint("PONS_VAULT_BUY_ETH_WEI");
        uint256 slippageBps = vm.envUint("PONS_VAULT_BUY_SLIPPAGE_BPS");
        uint256 ttl = vm.envUint("PONS_VAULT_BUY_TTL_SECONDS");
        (HooklessLPExecutor ex, PositionInspector ins, LaunchCursorToken q) = _coreFromEnv();
        _verifyExternal(strategy);
        _verifyCore(operator, ex, ins, q);
        if (!q.internalEndpoint(strategy)) revert PreflightFailed("launch strategy not allowlisted in Q");
        _verifyInitializedQPool(strategy, q);
        if (buyer.code.length == 0 || !q.internalEndpoint(buyer) ||
            HooklessQuoteBuyAdapter(payable(buyer)).source() != operator ||
            address(HooklessQuoteBuyAdapter(payable(buyer)).quoteToken()) != address(q) ||
            address(HooklessQuoteBuyAdapter(payable(buyer)).poolManager()) != POOL_MANAGER ||
            HooklessQuoteBuyAdapter(payable(buyer)).poolId() != _quoteEthPoolId(address(q)) ||
            HooklessQuoteBuyAdapter(payable(buyer)).fee() != Q_ETH_FEE ||
            HooklessQuoteBuyAdapter(payable(buyer)).tickSpacing() != Q_ETH_SPACING) {
            revert PreflightFailed("vault buyer binding mismatch");
        }
        if (ethIn == 0 || ethIn > uint256(uint128(type(int128).max)) ||
            slippageBps == 0 || slippageBps > 1_000 || ttl == 0 || ttl > 300 ||
            block.timestamp + ttl > type(uint64).max) {
            revert PreflightFailed("vault buy policy out of range");
        }
        (quotedQ,) = IBootstrapQuoter(QUOTER).quoteExactInputSingle(
            IBootstrapQuoter.QuoteExactSingleParams({
                poolKey: IBootstrapQuoter.PoolKey({
                    currency0: address(0), currency1: address(q), fee: Q_ETH_FEE,
                    tickSpacing: Q_ETH_SPACING, hooks: address(0)
                }), zeroForOne: true, exactAmount: uint128(ethIn), hookData: ""
            })
        );
        minQOut = quotedQ * (10_000 - slippageBps) / 10_000;
        if (minQOut == 0) revert PreflightFailed("Q buy quote is zero");
        deadline = uint64(block.timestamp + ttl);
        buyCalldata = abi.encodeCall(HooklessQuoteBuyAdapter.buyQ, (minQOut, address(ex), deadline));
    }

    function deployAfterLaunch()
        external returns (address priceGuard, address settlementRouter)
    {
        address operator = vm.envAddress("PONS_DEPLOYER");
        address strategy = vm.envAddress("PONS_INSTANT_STRATEGY");
        address developer = vm.envAddress("PONS_DEVELOPER");
        address configurator = vm.envAddress("PONS_PRICE_CONFIGURATOR");
        address exitConfigurator = vm.envAddress("PONS_EXIT_CONFIGURATOR");
        uint256 maxDeviation = vm.envUint("PONS_SPOT_MAX_DEVIATION_BPS");
        (HooklessLPExecutor ex, PositionInspector ins, LaunchCursorToken q) = _coreFromEnv();
        _verifyExternal(strategy);
        _verifyCore(operator, ex, ins, q);
        if (!q.internalEndpoint(strategy)) revert PreflightFailed("launch strategy not allowlisted in Q");
        _verifyLaunchedQ(strategy, q);
        if (developer == address(0) || developer == address(q) || developer == address(ex) ||
            developer == WIZARD_FANOUT || configurator == address(0) ||
            configurator == operator || exitConfigurator == address(0) ||
            exitConfigurator == operator || exitConfigurator == configurator) {
            revert PreflightFailed("developer or configurator invalid");
        }
        if (maxDeviation == 0 || maxDeviation > 1_000) {
            revert PreflightFailed("guard policy out of range");
        }
        if (ex.settlementRouter() != address(0)) {
            revert PreflightFailed("router already bound; reconcile receipts");
        }
        if (q.priceConfigurator() != operator || q.exitConfigurator() != operator || q.automaticEnabled()) {
            revert PreflightFailed("Q policy changed before post-launch binding");
        }

        vm.startBroadcast(operator);
        OpenPriceGuard spot = new OpenPriceGuard(
            PONS_FACTORY, STATE_VIEW, address(q), Q_ETH_FEE, Q_ETH_SPACING, uint16(maxDeviation)
        );
        ExitSettlementRouter router = new ExitSettlementRouter(
            address(ex), address(q), WETH, WIZARD_FANOUT, payable(developer), Q_ETH_FEE, Q_ETH_SPACING
        );
        ex.bindSettlementRouter(address(router));
        q.setInternalEndpoint(address(router), true);
        q.setInternalEndpoint(address(router.activeSale()), true);
        q.setInternalEndpoint(address(router.graduatedSale()), true);
        q.setInternalEndpoint(address(router.quoteBuy()), true);
        q.setPriceConfigurator(configurator);
        q.setExitConfigurator(exitConfigurator);
        vm.stopBroadcast();

        if (ex.settlementRouter() != address(router) || q.priceConfigurator() != configurator ||
            q.exitConfigurator() != exitConfigurator || q.automaticEnabled()) {
            revert PreflightFailed("post-launch binding mismatch");
        }
        return (address(spot), address(router));
    }

    /// @notice An explicit final gate after the Q/ETH launch has active
    /// liquidity and the watcher/keepers and recipients have been checked.
    /// It only enables transfer-triggered scheduling.
    function activate() external {
        address operator = vm.envAddress("PONS_DEPLOYER");
        address strategy = vm.envAddress("PONS_INSTANT_STRATEGY");
        address developer = vm.envAddress("PONS_DEVELOPER");
        address configurator = vm.envAddress("PONS_PRICE_CONFIGURATOR");
        address exitConfigurator = vm.envAddress("PONS_EXIT_CONFIGURATOR");
        (HooklessLPExecutor ex, PositionInspector ins, LaunchCursorToken q) = _coreFromEnv();
        _verifyExternal(strategy);
        _verifyCore(operator, ex, ins, q);
        if (!q.internalEndpoint(strategy)) revert PreflightFailed("launch strategy not allowlisted in Q");
        _verifyLaunchedQ(strategy, q);
        if (q.automaticEnabled() || q.priceConfigurator() != configurator ||
            q.exitConfigurator() != exitConfigurator || configurator == address(0) ||
            exitConfigurator == address(0) || configurator == operator ||
            exitConfigurator == operator || exitConfigurator == configurator) {
            revert PreflightFailed("cursor activation state invalid");
        }
        address router = ex.settlementRouter();
        if (router.code.length == 0 ||
            ExitSettlementRouter(payable(router)).developer() != developer ||
            ExitSettlementRouter(payable(router)).wizardFanout() != WIZARD_FANOUT ||
            address(ExitSettlementRouter(payable(router)).weth()) != WETH ||
            !q.internalEndpoint(router) || !q.internalEndpoint(address(ExitSettlementRouter(payable(router)).activeSale())) ||
            !q.internalEndpoint(address(ExitSettlementRouter(payable(router)).graduatedSale())) ||
            !q.internalEndpoint(address(ExitSettlementRouter(payable(router)).quoteBuy()))) {
            revert PreflightFailed("router, recipients, or endpoints changed");
        }
        vm.startBroadcast(operator);
        q.setAutomatic(true);
        vm.stopBroadcast();
        if (!q.automaticEnabled()) revert PreflightFailed("activation failed");
    }

    function _coreFromEnv()
        private returns (HooklessLPExecutor ex, PositionInspector ins, LaunchCursorToken q)
    {
        ex = HooklessLPExecutor(payable(vm.envAddress("PONS_EXECUTOR_ADDRESS")));
        ins = PositionInspector(vm.envAddress("PONS_INSPECTOR_ADDRESS"));
        q = LaunchCursorToken(vm.envAddress("PONS_Q_ADDRESS"));
    }

    function _verifyExternal(address strategy) private view {
        if (block.chainid != CHAIN_ID) revert PreflightFailed("wrong chain");
        if (strategy != INSTANT_FEES_ON && strategy != INSTANT_FEES_OFF) {
            revert PreflightFailed("strategy is not current Robinhood Instant Launch");
        }
        address[13] memory required = [
            POOL_MANAGER, POSITION_MANAGER, STATE_VIEW, QUOTER, PERMIT2, PONS_FACTORY,
            LAUNCHER, strategy, FEES_ON_SPLITTER, FEES_OFF_SPLITTER, BENEFICIARY_VAULT,
            WETH, WIZARD_FANOUT
        ];
        for (uint256 i; i < required.length; ++i) {
            if (required[i].code.length == 0) revert PreflightFailed("canonical address has no code");
        }
        if (WIZARD_FANOUT.codehash != WIZARD_FANOUT_CODEHASH) {
            revert PreflightFailed("wizard fanout code changed");
        }
        if (IBootstrapPoolLinked(POSITION_MANAGER).poolManager() != POOL_MANAGER ||
            IBootstrapPoolLinked(STATE_VIEW).poolManager() != POOL_MANAGER ||
            IBootstrapPoolLinked(QUOTER).poolManager() != POOL_MANAGER ||
            IBootstrapPonsFactory(PONS_FACTORY).poolManager() != POOL_MANAGER ||
            IBootstrapLauncher(LAUNCHER).permit2() != PERMIT2) {
            revert PreflightFailed("canonical deployment binding mismatch");
        }
        address hook = IBootstrapPonsFactory(PONS_FACTORY).memeHook();
        if (hook.code.length == 0 || IBootstrapPonsHook(hook).factory() != PONS_FACTORY ||
            IBootstrapPonsHook(hook).poolManager() != POOL_MANAGER) {
            revert PreflightFailed("Pons hook binding mismatch");
        }
        IBootstrapInstantStrategy instant = IBootstrapInstantStrategy(strategy);
        address splitter = strategy == INSTANT_FEES_ON ? FEES_ON_SPLITTER : FEES_OFF_SPLITTER;
        address vault = strategy == INSTANT_FEES_ON ? BENEFICIARY_VAULT : address(0);
        if (instant.launcher() != LAUNCHER || instant.poolManager() != POOL_MANAGER ||
            instant.positionManager() != POSITION_MANAGER || instant.feeSplitter() != splitter ||
            instant.beneficiaryVault() != vault || instant.TOTAL_SUPPLY() != SUPPLY ||
            instant.LP_FEE() != Q_ETH_FEE || instant.TICK_SPACING() != Q_ETH_SPACING ||
            instant.initialTick() != 198_050) {
            revert PreflightFailed("Instant Launch variant mismatch");
        }
        if (IBootstrapInstantStrategy(strategy).feeSplitter() != splitter ||
            _positionManagerOf(splitter) != POSITION_MANAGER ||
            (vault != address(0) && _positionManagerOf(vault) != POSITION_MANAGER)) {
            revert PreflightFailed("fee recipient binding mismatch");
        }
    }

    function _positionManagerOf(address target) private view returns (address) {
        (bool ok, bytes memory ret) = target.staticcall(abi.encodeWithSignature("positionManager()"));
        if (!ok || ret.length != 32) revert PreflightFailed("missing positionManager getter");
        return abi.decode(ret, (address));
    }

    function _verifyCore(address operator, HooklessLPExecutor ex, PositionInspector ins, LaunchCursorToken q)
        private view
    {
        _requireNonzero(operator, "operator");
        if (address(ex).code.length == 0 || address(ins).code.length == 0 || address(q).code.length == 0 ||
            ex.owner() != operator || q.owner() != operator ||
            q.decimals() != 18 || q.INITIAL_SUPPLY() != SUPPLY || q.OPEN_MINT_BPS() != 100 ||
            ex.controller() != address(q) || ex.quoteToken() != address(q) ||
            address(ex.positionManager()) != POSITION_MANAGER || address(ex.stateView()) != STATE_VIEW ||
            address(ex.permit2()) != PERMIT2 || ex.poolManager() != POOL_MANAGER ||
            address(ins.executor()) != address(ex) || address(ins.cursor()) != address(q) ||
            address(q.executor()) != address(ex) || q.exitNotifier() != address(ins) ||
            address(q.ponsFactory()) != PONS_FACTORY || address(q.feePolicy().controller()) != address(q) ||
            !q.internalEndpoint(PERMIT2) || !q.internalEndpoint(LAUNCHER)) {
            revert PreflightFailed("core binding mismatch");
        }
    }

    function _verifyLaunchedQ(address strategy, LaunchCursorToken q) private view {
        _verifyInitializedQPool(strategy, q);
        bytes32 poolId = _quoteEthPoolId(address(q));
        if (IBootstrapStateView(STATE_VIEW).getLiquidity(poolId) == 0) {
            revert PreflightFailed("Q/ETH has no active liquidity yet");
        }
    }

    function _verifyInitializedQPool(address strategy, LaunchCursorToken q) private view {
        bytes32 poolId = _quoteEthPoolId(address(q));
        (uint160 sqrtPriceX96,,, uint24 liveFee) = IBootstrapStateView(STATE_VIEW).getSlot0(poolId);
        if (sqrtPriceX96 < MIN_SQRT_PRICE || sqrtPriceX96 >= MAX_SQRT_PRICE ||
            liveFee != Q_ETH_FEE || q.totalSupply() == 0 || q.totalSupply() > SUPPLY ||
            q.balanceOf(LAUNCHER) != 0 || q.balanceOf(strategy) != 0 || q.automaticEnabled()) {
            revert PreflightFailed("Q/ETH launch or Q state not ready");
        }
    }

    function _quoteEthPoolId(address q) private pure returns (bytes32) {
        return keccak256(abi.encode(address(0), q, Q_ETH_FEE, Q_ETH_SPACING, address(0)));
    }

    function _requireNonzero(address value, string memory label) private pure {
        if (value == address(0)) revert PreflightFailed(label);
    }

    function _pinnedMetadata(string memory key, string memory expected) private returns (string memory) {
        try vm.envString(key) returns (string memory supplied) {
            if (keccak256(bytes(supplied)) != keccak256(bytes(expected))) {
                revert PreflightFailed("Q metadata differs from pinned wizard launch");
            }
            return supplied;
        } catch {
            return expected;
        }
    }
}
