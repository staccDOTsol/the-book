// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

/// @notice The ABI shape of Uniswap v4's PoolKey. Currency and IHooks are
/// address-valued types, so their external ABI is identical to address.
interface IHooklessPositionManager {
    struct PoolKey {
        address currency0;
        address currency1;
        uint24 fee;
        int24 tickSpacing;
        address hooks;
    }

    function poolManager() external view returns (address);
    function initializePool(PoolKey calldata key, uint160 sqrtPriceX96) external payable returns (int24);
    function modifyLiquidities(bytes calldata unlockData, uint256 deadline) external payable;
    function multicall(bytes[] calldata data) external payable returns (bytes[] memory results);
    function nextTokenId() external view returns (uint256);
    function ownerOf(uint256 tokenId) external view returns (address);
    function getPositionLiquidity(uint256 tokenId) external view returns (uint128);
}

interface IHooklessStateView {
    function poolManager() external view returns (address);
    function getSlot0(bytes32 poolId)
        external view returns (uint160 sqrtPriceX96, int24 tick, uint24 protocolFee, uint24 lpFee);
}

interface IHooklessPermit2 {
    function approve(address token, address spender, uint160 amount, uint48 expiration) external;
}

interface IHooklessERC20 {
    function balanceOf(address account) external view returns (uint256);
    function allowance(address owner, address spender) external view returns (uint256);
    function approve(address spender, uint256 amount) external returns (bool);
    function transfer(address to, uint256 amount) external returns (bool);
}

interface IHooklessCursorQ {
    function executor() external view returns (address);
    function ponsFactory() external view returns (address);
    function totalSupply() external view returns (uint256);
    function burn(uint256 amount) external;
}

interface IHooklessPriceGuard {
    function ponsFactory() external view returns (address);
    function stateView() external view returns (address);
    function quoteToken() external view returns (address);
    function quoteEthPoolId() external view returns (bytes32);
    function validate(address token, uint160 proposedSqrtPriceX96) external view returns (uint160 referenceSqrtPriceX96);
}

interface IHooklessExecutableDepthGuard {
    function matches(address source, address priceGuard, address settlementRouter) external view returns (bool);
    function validate(address token, uint128 liquidity, int24 tickLower, int24 tickUpper)
        external returns (uint256 maxX, uint256 executableQ, uint256 safeQ, uint256 requiredQ);
}

interface IHooklessExitSaleAdapter {
    function source() external view returns (address);
    function factory() external view returns (address);
}

interface IHooklessExitQuoteBuy {
    function source() external view returns (address);
    function poolManager() external view returns (address);
    function quoteToken() external view returns (address);
    function poolId() external view returns (bytes32);
}

interface IHooklessSettlementRouter {
    function source() external view returns (address);
    function quoteToken() external view returns (address);
    function factory() external view returns (address);
    function weth() external view returns (address);
    function wizardFanout() external view returns (address);
    function developer() external view returns (address);
    function activeSale() external view returns (address);
    function graduatedSale() external view returns (address);
    function quoteBuy() external view returns (address);
    function settle(address token, uint256 xAmount, uint256 qAmount, uint256 minEthOut, uint256 minQOut, uint64 deadline)
        external returns (uint256 ethOut, uint256 qBurned);
}

/// @dev Exact getSqrtPriceAtTick calculation from Uniswap v4 TickMath. Kept
/// local so this prototype needs no external Solidity packages to compile.
library HooklessTickMath {
    error InvalidTick(int24 tick);

    int24 internal constant MIN_TICK = -887272;
    int24 internal constant MAX_TICK = 887272;
    uint160 internal constant MIN_SQRT_PRICE = 4295128739;
    uint160 internal constant MAX_SQRT_PRICE = 1461446703485210103287273052203988822378723970342;

    function getSqrtPriceAtTick(int24 tick) internal pure returns (uint160 sqrtPriceX96) {
        unchecked {
            uint256 absTick;
            assembly ("memory-safe") {
                tick := signextend(2, tick)
                let mask := sar(255, tick)
                absTick := xor(mask, add(mask, tick))
            }
            if (absTick > uint256(int256(MAX_TICK))) revert InvalidTick(tick);

            uint256 price;
            assembly ("memory-safe") {
                price := xor(shl(128, 1), mul(xor(shl(128, 1), 0xfffcb933bd6fad37aa2d162d1a594001), and(absTick, 0x1)))
            }
            if (absTick & 0x2 != 0) price = (price * 0xfff97272373d413259a46990580e213a) >> 128;
            if (absTick & 0x4 != 0) price = (price * 0xfff2e50f5f656932ef12357cf3c7fdcc) >> 128;
            if (absTick & 0x8 != 0) price = (price * 0xffe5caca7e10e4e61c3624eaa0941cd0) >> 128;
            if (absTick & 0x10 != 0) price = (price * 0xffcb9843d60f6159c9db58835c926644) >> 128;
            if (absTick & 0x20 != 0) price = (price * 0xff973b41fa98c081472e6896dfb254c0) >> 128;
            if (absTick & 0x40 != 0) price = (price * 0xff2ea16466c96a3843ec78b326b52861) >> 128;
            if (absTick & 0x80 != 0) price = (price * 0xfe5dee046a99a2a811c461f1969c3053) >> 128;
            if (absTick & 0x100 != 0) price = (price * 0xfcbe86c7900a88aedcffc83b479aa3a4) >> 128;
            if (absTick & 0x200 != 0) price = (price * 0xf987a7253ac413176f2b074cf7815e54) >> 128;
            if (absTick & 0x400 != 0) price = (price * 0xf3392b0822b70005940c7a398e4b70f3) >> 128;
            if (absTick & 0x800 != 0) price = (price * 0xe7159475a2c29b7443b29c7fa6e889d9) >> 128;
            if (absTick & 0x1000 != 0) price = (price * 0xd097f3bdfd2022b8845ad8f792aa5825) >> 128;
            if (absTick & 0x2000 != 0) price = (price * 0xa9f746462d870fdf8a65dc1f90e061e5) >> 128;
            if (absTick & 0x4000 != 0) price = (price * 0x70d869a156d2a1b890bb3df62baf32f7) >> 128;
            if (absTick & 0x8000 != 0) price = (price * 0x31be135f97d08fd981231505542fcfa6) >> 128;
            if (absTick & 0x10000 != 0) price = (price * 0x9aa508b5b7a84e1c677de54f3e99bc9) >> 128;
            if (absTick & 0x20000 != 0) price = (price * 0x5d6af8dedb81196699c329225ee604) >> 128;
            if (absTick & 0x40000 != 0) price = (price * 0x2216e584f5fa1ea926041bedfe98) >> 128;
            if (absTick & 0x80000 != 0) price = (price * 0x48a170391f7dc42444e8fa2) >> 128;

            assembly ("memory-safe") {
                if sgt(tick, 0) { price := div(not(0), price) }
                sqrtPriceX96 := shr(32, add(price, sub(shl(32, 1), 1)))
            }
        }
    }
}

/// @notice Custodian for one hookless X/Q pool and three newly-minted-Q v4
/// positions per Pons launch X. The configured starting price and ranges are
/// deterministic X/Q terms; the pool's later price is set by swaps.
contract HooklessLPExecutor {
    using HooklessTickMath for int24;

    uint8 private constant MINT_POSITION = 0x02;
    uint8 private constant DECREASE_LIQUIDITY = 0x01;
    uint8 private constant BURN_POSITION = 0x03;
    uint8 private constant SETTLE_PAIR = 0x0d;
    uint8 private constant TAKE_PAIR = 0x11;

    struct OpenConfig {
        uint160 startingSqrtPriceX96;
        uint128[3] liquidity;
        uint128[3] maxQuoteIn;
        int24 tickSpacing;
        int24[3] tickLower;
        int24[3] tickUpper;
        uint64 deadline;
    }

    struct ExitConfig {
        uint8 tranche;
        uint128 minTokenOut;
        uint128 minQuoteOut;
        uint256 minEthOut;
        uint256 minQOut;
        uint64 deadline;
        bool timed;
    }

    struct HarvestConfig {
        uint128 minTokenFee;
        uint128 minQuoteFee;
        uint256 minEthOut;
        uint256 minQOut;
        uint256 grossEthValue;
        uint32 estimatedGasUnits;
        uint64 deadline;
    }

    struct HarvestedAmounts {
        uint256 tokenAmount;
        uint256 quoteAmount;
    }

    struct HarvestSnapshot {
        uint256 tokenBefore;
        uint256 quoteBefore;
        uint256 nativeBefore;
        uint256 supplyBefore;
        uint256 newToken;
        uint256 pendingToken;
        uint256 pendingQuote;
        uint256 totalQuote;
        uint256 tokenToSettle;
    }

    struct ExitSnapshot {
        uint256 tokenBefore;
        uint256 quoteBefore;
        uint256 nativeBefore;
        uint256 supplyBefore;
        uint256 harvestedToken;
        uint256 harvestedQuote;
        uint256 tokenToSettle;
        uint256 quoteToSettle;
    }

    struct Position {
        uint256 tokenId;
        bytes32 poolId;
        uint24 feePips;
        int24 tickSpacing;
        int24 tickLower;
        int24 tickUpper;
        bool active;
        bool enteredBand;
        uint256 quoteSpent;
        uint256 tokenWithdrawn;
        uint256 quoteWithdrawn;
    }

    address public immutable owner;
    address public controller;
    address public quoteToken;
    address public settlementRouter;
    address public immutable poolManager;
    IHooklessPositionManager public immutable positionManager;
    IHooklessStateView public immutable stateView;
    IHooklessPermit2 public immutable permit2;

    mapping(address token => OpenConfig) public openConfigs;
    mapping(address token => ExitConfig) public exitConfigs;
    mapping(address token => HarvestConfig) public harvestConfigs;
    mapping(address token => HarvestedAmounts) public harvestedAmounts;
    mapping(address token => Position) public positions;
    mapping(address token => mapping(uint8 tranche => Position)) public extraPositions;
    uint256 public reservedHarvestedQuote;

    uint256 private _locked = 1;

    event ControllerBound(address indexed controller);
    event SettlementRouterBound(address indexed router);
    event OpenConfigured(address indexed token, uint160 startingSqrtPriceX96, int24 tickLower, int24 tickUpper);
    event ExitConfigured(
        address indexed token, uint128 minTokenOut, uint128 minQuoteOut,
        uint256 minEthOut, uint256 minQOut, uint64 deadline
    );
    event PositionOpened(address indexed token, bytes32 indexed poolId, uint256 indexed tokenId, uint24 feePips, uint256 quoteSpent);
    event TrancheOpened(address indexed token, uint8 indexed tranche, uint256 indexed tokenId, uint256 quoteSpent);
    event BandEntered(address indexed token, uint256 indexed tokenId);
    event FeesCollected(address indexed token, uint256 tokenAmount, uint256 quoteAmount);
    event FeesSettled(address indexed token, uint256 tokenAmount, uint256 quoteAmount, uint256 ethOut, uint256 quoteBurned);
    event PositionWithdrawn(address indexed token, uint256 tokenAmount, uint256 quoteAmount);
    event PositionSettled(address indexed token, uint256 tokenSettled, uint256 quoteSettled, uint256 ethOut, uint256 quoteBurned);
    event TrancheSettled(address indexed token, uint8 indexed tranche, uint256 indexed tokenId);
    event PositionRescued(address indexed token, address indexed recipient, uint256 tokenAmount, uint256 quoteAmount);
    event HeldAssetRescued(address indexed token, address indexed recipient, uint256 amount);

    error NotController();
    error NotOwner();
    error ReentrantCall();
    error InvalidConfiguration();
    error AlreadyOpened();
    error NotActive();
    error NotInBand();
    error NotAtBoundary();
    error NotEnteredBand();
    error AlreadyInitialized();
    error WrongPoolState();
    error WrongPosition();
    error NoTokensReceived();
    error ApprovalFailed();
    error UnsettledHarvest();
    error WrongSettlement();
    error OutcomeUnavailable();

    function _position(address token, uint8 tranche) private view returns (Position storage position) {
        if (tranche > 2) revert InvalidConfiguration();
        if (tranche == 0) return positions[token];
        return extraPositions[token][tranche];
    }

    function activePositionCount(address token) external view returns (uint8) {
        return _activeCount(token);
    }

    function _activeCount(address token) private view returns (uint8 count) {
        for (uint8 i; i < 3; ++i) if (_position(token, i).active) ++count;
    }

    modifier onlyController() {
        if (msg.sender != controller) revert NotController();
        _;
    }

    modifier nonReentrant() {
        if (_locked != 1) revert ReentrantCall();
        _locked = 2;
        _;
        _locked = 1;
    }

    constructor(
        address poolManager_,
        address positionManager_,
        address stateView_,
        address permit2_
    ) {
        if (
            poolManager_.code.length == 0 || positionManager_.code.length == 0 ||
            stateView_.code.length == 0 || permit2_.code.length == 0 ||
            IHooklessPositionManager(positionManager_).poolManager() != poolManager_ ||
            IHooklessStateView(stateView_).poolManager() != poolManager_
        ) revert InvalidConfiguration();
        owner = msg.sender;
        poolManager = poolManager_;
        positionManager = IHooklessPositionManager(positionManager_);
        stateView = IHooklessStateView(stateView_);
        permit2 = IHooklessPermit2(permit2_);
    }

    /// @notice One-time bootstrap: deploy this vault, deploy Q pointing at it,
    /// then bind the deployed Q contract as both controller and quote asset.
    function bindController(address quoteToken_) external nonReentrant {
        if (msg.sender != owner) revert NotOwner();
        if (
            controller != address(0) || quoteToken_.code.length == 0 ||
            IHooklessCursorQ(quoteToken_).executor() != address(this)
        ) revert InvalidConfiguration();
        controller = quoteToken_;
        quoteToken = quoteToken_;
        emit ControllerBound(quoteToken_);
    }

    /// @notice One-time bind after Q and its launch pool exist. The router
    /// constructs its own sale and quote-buy adapters and validates them.
    function bindSettlementRouter(address router) external nonReentrant {
        if (msg.sender != owner) revert NotOwner();
        if (settlementRouter != address(0) || controller == address(0) || router.code.length == 0) {
            revert InvalidConfiguration();
        }
        IHooklessSettlementRouter route = IHooklessSettlementRouter(router);
        if (
            route.source() != address(this) || route.quoteToken() != quoteToken ||
            route.factory() != IHooklessCursorQ(controller).ponsFactory() ||
            route.wizardFanout().code.length == 0 || route.developer() == address(0) ||
            route.quoteBuy().code.length == 0
        ) revert InvalidConfiguration();
        settlementRouter = router;
        emit SettlementRouterBound(router);
    }

    /// @notice The controller must commit bounded price, range and spend data
    /// before open. Live executable depth is checked again inside open. A
    /// later retry uses this same configuration until the controller changes
    /// it, but the fee is supplied separately by the controller.
    function configureOpen(address token, bytes calldata encodedPlan) external onlyController nonReentrant {
        OpenConfig memory config = abi.decode(encodedPlan, (OpenConfig));
        if (
            token == address(0) || token == quoteToken || token.code.length == 0 ||
            positions[token].tokenId != 0
        ) {
            revert InvalidConfiguration();
        }
        if (
            config.deadline < block.timestamp || config.deadline > block.timestamp + 15 minutes ||
            config.tickSpacing < 1 || config.tickSpacing > type(int16).max ||
            config.startingSqrtPriceX96 < HooklessTickMath.MIN_SQRT_PRICE ||
            config.startingSqrtPriceX96 >= HooklessTickMath.MAX_SQRT_PRICE
        ) revert InvalidConfiguration();
        for (uint8 i; i < 3; ++i) {
            if (
                config.liquidity[i] == 0 || config.maxQuoteIn[i] == 0 ||
                config.tickLower[i] < HooklessTickMath.MIN_TICK ||
                config.tickUpper[i] > HooklessTickMath.MAX_TICK ||
                config.tickLower[i] >= config.tickUpper[i] ||
                config.tickLower[i] % config.tickSpacing != 0 ||
                config.tickUpper[i] % config.tickSpacing != 0
            ) revert InvalidConfiguration();
            uint160 lower = config.tickLower[i].getSqrtPriceAtTick();
            uint160 upper = config.tickUpper[i].getSqrtPriceAtTick();
            if (!_isQuoteOnly(token, config.startingSqrtPriceX96, lower, upper)) revert InvalidConfiguration();
        }
        openConfigs[token] = config;
        emit OpenConfigured(token, config.startingSqrtPriceX96, config.tickLower[0], config.tickUpper[2]);
    }

    /// @notice Commit short-lived burn and swap minima. The controller only
    /// forwards this after the inspector has queued a boundary exit.
    function configureExit(address token, bytes calldata encodedConfig) external onlyController nonReentrant {
        ExitConfig memory config = abi.decode(encodedConfig, (ExitConfig));
        Position storage position = _position(token, config.tranche);
        if (
            !position.active || (!config.timed && !position.enteredBand) ||
            settlementRouter == address(0) ||
            (config.minTokenOut == 0 && config.minQuoteOut == 0) ||
            (config.minEthOut == 0) != (config.minQOut == 0) ||
            config.deadline < block.timestamp || config.deadline > block.timestamp + 15 minutes
        ) revert InvalidConfiguration();
        exitConfigs[token] = config;
        emit ExitConfigured(
            token, config.minTokenOut, config.minQuoteOut,
            config.minEthOut, config.minQOut, config.deadline
        );
    }

    /// @notice Short-lived, signer-quoted fee claim. The signer supplies an
    /// executable ETH valuation and whole-cycle gas estimate; harvest checks
    /// the actual collected minimums and settlement bounds in the same call.
    /// No claim depends on the position reaching an exit boundary.
    function configureHarvest(address token, HarvestConfig calldata config) external onlyController nonReentrant {
        if (
            _activeCount(token) == 0 || settlementRouter == address(0) ||
            config.deadline < block.timestamp || config.deadline > block.timestamp + 15 minutes ||
            config.grossEthValue == 0 || config.estimatedGasUnits == 0 ||
            (config.minTokenFee == 0 && config.minQuoteFee == 0) ||
            (config.minTokenFee == 0 && (config.minEthOut != 0 || config.minQOut != 0)) ||
            (config.minTokenFee != 0 && (config.minEthOut == 0 || config.minQOut == 0))
        ) revert InvalidConfiguration();
        harvestConfigs[token] = config;
    }

    /// @notice Creates a new zero-hook, static-fee pool and Q-only position in
    /// the same PositionManager multicall. The controller first mints a fixed
    /// fraction of current Q supply to this vault in the same atomic call.
    /// `feePips` is the controller's frozen selection for this token.
    function open(address token, uint24 feePips, uint256[3] calldata mintedQuote)
        external onlyController nonReentrant returns (uint256[3] memory quoteSpent)
    {
        if (positions[token].tokenId != 0) revert AlreadyOpened();
        OpenConfig memory config = openConfigs[token];
        if (
            config.liquidity[0] == 0 || config.deadline < block.timestamp ||
            feePips < 50_000 || feePips > 500_000 || quoteToken.code.length == 0 ||
            settlementRouter == address(0)
        ) revert InvalidConfiguration();
        uint256 totalMinted;
        for (uint8 i; i < 3; ++i) {
            if (mintedQuote[i] == 0 || config.maxQuoteIn[i] > mintedQuote[i]) revert InvalidConfiguration();
            totalMinted += mintedQuote[i];
        }

        IHooklessPositionManager.PoolKey memory key = _poolKey(token, feePips, config.tickSpacing);
        bytes32 poolId = keccak256(abi.encode(key));
        (uint160 beforePrice,,,) = stateView.getSlot0(poolId);
        if (beforePrice != 0) revert AlreadyInitialized();

        uint256 quoteBefore = IHooklessERC20(quoteToken).balanceOf(address(this));
        uint256 tokenBefore = IHooklessERC20(token).balanceOf(address(this));
        if (quoteBefore < totalMinted || quoteBefore - totalMinted < reservedHarvestedQuote) {
            revert InvalidConfiguration();
        }
        positionManager.initializePool(key, config.startingSqrtPriceX96);
        for (uint8 i; i < 3; ++i) {
            uint256 tokenId = positionManager.nextTokenId();
            quoteSpent[i] = _mintTranche(token, key, config, i);
            Position storage position = _position(token, i);
            position.tokenId = tokenId;
            position.poolId = poolId;
            position.feePips = feePips;
            position.tickSpacing = config.tickSpacing;
            position.tickLower = config.tickLower[i];
            position.tickUpper = config.tickUpper[i];
            position.active = true;
            position.quoteSpent = quoteSpent[i];
            emit PositionOpened(token, poolId, tokenId, feePips, quoteSpent[i]);
            emit TrancheOpened(token, i, tokenId, quoteSpent[i]);
        }
        (uint160 afterPrice,,, uint24 liveFee) = stateView.getSlot0(poolId);
        if (afterPrice != config.startingSqrtPriceX96 || liveFee != feePips ||
            IHooklessERC20(token).balanceOf(address(this)) != tokenBefore) revert WrongPoolState();
        delete openConfigs[token];
        uint256 totalSpent = quoteSpent[0] + quoteSpent[1] + quoteSpent[2];
        if (quoteBefore - IHooklessERC20(quoteToken).balanceOf(address(this)) != totalSpent) {
            revert WrongPosition();
        }
        if (totalMinted > totalSpent) IHooklessCursorQ(quoteToken).burn(totalMinted - totalSpent);
    }

    /// @notice Exact live range state. `atQuoteBoundary` can be true straight
    /// after open; the stored `enteredBand` prevents an immediate exit then.
    /// A move from the initial Q-only side through the entire band to the
    /// all-X side proves entry even if no interior block was inspected. Fees
    /// are excluded from these principal-inventory boundary flags.
    function inspect(address token)
        public view returns (
            uint160 sqrtPriceX96,
            bool inBand,
            bool atQuoteBoundary,
            bool atTokenBoundary,
            bool enteredBand
        )
    {
        return inspectTranche(token, 0);
    }

    function inspectTranche(address token, uint8 tranche)
        public view returns (
            uint160 sqrtPriceX96,
            bool inBand,
            bool atQuoteBoundary,
            bool atTokenBoundary,
            bool enteredBand
        )
    {
        Position storage position = _position(token, tranche);
        if (!position.active) revert NotActive();
        (sqrtPriceX96,,,) = stateView.getSlot0(position.poolId);
        uint160 lower = position.tickLower.getSqrtPriceAtTick();
        uint160 upper = position.tickUpper.getSqrtPriceAtTick();
        if (sqrtPriceX96 == 0) revert WrongPoolState();
        inBand = lower < sqrtPriceX96 && sqrtPriceX96 < upper;
        atQuoteBoundary = _isQuoteOnly(token, sqrtPriceX96, lower, upper);
        atTokenBoundary = _isTokenOnly(token, sqrtPriceX96, lower, upper);
        enteredBand = position.enteredBand;
    }

    /// @notice Anyone may record a current strict-interior observation, or a
    /// full crossing to the all-X side from the verified Q-only initial side.
    /// The latter handles a single swap traversing the whole position band.
    function markEntered(address token) external nonReentrant {
        _markEntered(token, 0);
    }

    function markEnteredTranche(address token, uint8 tranche) external nonReentrant {
        _markEntered(token, tranche);
    }

    function _markEntered(address token, uint8 tranche) private {
        Position storage position = _position(token, tranche);
        if (!position.active) revert NotActive();
        if (position.enteredBand) return;
        (, bool inBand,, bool atTokenBoundary,) = inspectTranche(token, tranche);
        if (!inBand && !atTokenBoundary) revert NotInBand();
        position.enteredBand = true;
        emit BandEntered(token, position.tokenId);
    }

    /// @notice Simulate a zero-liquidity fee collection with eth_call from Q.
    /// This is deliberately non-view: the offchain call runs the real v4
    /// PositionManager path and discards its state at the end of eth_call.
    /// The Q contract never forwards an onchain call to this method.
    function simulateHarvest(address token)
        external onlyController nonReentrant returns (uint256 tokenAmount, uint256 quoteAmount)
    {
        if (_activeCount(token) == 0) revert NotActive();
        (tokenAmount, quoteAmount) = _collectFees(token);
        HarvestedAmounts memory pending = harvestedAmounts[token];
        tokenAmount += pending.tokenAmount;
        quoteAmount += pending.quoteAmount;
    }

    /// @notice Collect fees without touching LP principal, then settle the
    /// executable X and all Q now. A Q-only claim burns Q. If Q fees alone pay
    /// for gas but X fees cannot yet be sold, X remains reserved for a later
    /// harvest or the boundary exit.
    function harvest(address token) external onlyController nonReentrant {
        if (_activeCount(token) == 0) revert NotActive();
        HarvestConfig memory config = harvestConfigs[token];
        if (config.deadline < block.timestamp || config.deadline > block.timestamp + 15 minutes) {
            revert InvalidConfiguration();
        }
        HarvestSnapshot memory snapshot;
        snapshot.tokenBefore = IHooklessERC20(token).balanceOf(address(this));
        snapshot.quoteBefore = IHooklessERC20(quoteToken).balanceOf(address(this));
        snapshot.nativeBefore = address(this).balance;
        snapshot.supplyBefore = IHooklessCursorQ(quoteToken).totalSupply();
        HarvestedAmounts memory pending = harvestedAmounts[token];
        snapshot.pendingToken = pending.tokenAmount;
        snapshot.pendingQuote = pending.quoteAmount;
        uint256 newQuote;
        (snapshot.newToken, newQuote) = _collectFees(token);
        uint256 totalToken = snapshot.newToken + snapshot.pendingToken;
        snapshot.totalQuote = newQuote + snapshot.pendingQuote;
        if (totalToken < config.minTokenFee || snapshot.totalQuote < config.minQuoteFee) {
            revert NoTokensReceived();
        }
        snapshot.tokenToSettle = config.minTokenFee == 0 ? 0 : totalToken;
        if (snapshot.tokenToSettle == 0 && snapshot.totalQuote == 0) revert NoTokensReceived();
        (uint256 ethOut, uint256 quoteBurned) = _routeHarvest(token, config, snapshot);
        harvestedAmounts[token].tokenAmount = totalToken - snapshot.tokenToSettle;
        harvestedAmounts[token].quoteAmount = 0;
        reservedHarvestedQuote -= snapshot.pendingQuote;
        delete harvestConfigs[token];
        emit FeesSettled(token, snapshot.tokenToSettle, snapshot.totalQuote, ethOut, quoteBurned);
    }

    function _routeHarvest(address token, HarvestConfig memory config, HarvestSnapshot memory snapshot)
        private returns (uint256 ethOut, uint256 quoteBurned)
    {
        address router = settlementRouter;
        _approveExact(token, router, snapshot.tokenToSettle);
        _approveExact(quoteToken, router, snapshot.totalQuote);
        (ethOut, quoteBurned) = IHooklessSettlementRouter(router).settle(
            token, snapshot.tokenToSettle, snapshot.totalQuote,
            config.minEthOut, config.minQOut, config.deadline
        );
        _clearApproval(token, router, snapshot.tokenToSettle);
        _clearApproval(quoteToken, router, snapshot.totalQuote);
        if (
            (snapshot.tokenToSettle == 0 && (ethOut != 0 || quoteBurned != snapshot.totalQuote)) ||
            (snapshot.tokenToSettle != 0 &&
                (ethOut < config.minEthOut || quoteBurned < snapshot.totalQuote + config.minQOut)) ||
            IHooklessERC20(token).balanceOf(address(this)) !=
                snapshot.tokenBefore + snapshot.newToken - snapshot.tokenToSettle ||
            IHooklessERC20(quoteToken).balanceOf(address(this)) !=
                snapshot.quoteBefore - snapshot.pendingQuote ||
            address(this).balance != snapshot.nativeBefore ||
            IHooklessCursorQ(quoteToken).totalSupply() != snapshot.supplyBefore - quoteBurned
        ) revert WrongSettlement();
    }

    function _collectFees(address token) private returns (uint256 tokenAmount, uint256 quoteAmount) {
        uint256 tokenBefore = IHooklessERC20(token).balanceOf(address(this));
        uint256 quoteBefore = IHooklessERC20(quoteToken).balanceOf(address(this));
        uint8 count;
        for (uint8 i; i < 3; ++i) {
            Position storage position = _position(token, i);
            if (!position.active) continue;
            ++count;
            IHooklessPositionManager.PoolKey memory key = _poolKey(token, position.feePips, position.tickSpacing);
            bytes[] memory params = new bytes[](2);
            params[0] = abi.encode(position.tokenId, uint256(0), uint128(0), uint128(0), bytes(""));
            params[1] = abi.encode(key.currency0, key.currency1, address(this));
            positionManager.modifyLiquidities(
                abi.encode(abi.encodePacked(DECREASE_LIQUIDITY, TAKE_PAIR), params), block.timestamp
            );
        }
        if (count == 0) revert NotActive();
        tokenAmount = IHooklessERC20(token).balanceOf(address(this)) - tokenBefore;
        quoteAmount = IHooklessERC20(quoteToken).balanceOf(address(this)) - quoteBefore;
        emit FeesCollected(token, tokenAmount, quoteAmount);
    }

    function previewHarvest(address token) external view returns (uint256 grossEthValue, uint256 estimatedGasUnits) {
        if (_activeCount(token) == 0) revert NotActive();
        HarvestConfig memory config = harvestConfigs[token];
        if (config.deadline < block.timestamp || config.grossEthValue == 0 || config.estimatedGasUnits == 0) {
            revert OutcomeUnavailable();
        }
        return (config.grossEthValue, config.estimatedGasUnits);
    }

    /// @notice Mechanically withdraws and collects everything from a position.
    /// The proceeds remain in this vault for a later liquidation/burn/payout
    /// implementation. Minima apply to principal; fee receipts can add more.
    function withdrawTranche(address token, uint8 tranche, uint128 minTokenOut, uint128 minQuoteOut, uint64 deadline)
        external onlyController nonReentrant returns (uint256 tokenAmount, uint256 quoteAmount)
    {
        Position storage position = _position(token, tranche);
        if (!position.active) revert NotActive();
        if (!position.enteredBand) revert NotEnteredBand();
        (, , bool atQuoteBoundary, bool atTokenBoundary,) = inspectTranche(token, tranche);
        if (!atQuoteBoundary && !atTokenBoundary) revert NotAtBoundary();
        if ((atQuoteBoundary && minQuoteOut == 0) || (atTokenBoundary && minTokenOut == 0)) {
            revert InvalidConfiguration();
        }
        return _burnPosition(token, tranche, minTokenOut, minQuoteOut, deadline);
    }

    /// @notice Offchain eth_call helper for a Q-authorized 120-minute unwind.
    /// It simulates a real NFT burn at any tick and returns the two receipts;
    /// an eth_call discards the temporary state. Q does not forward normal
    /// onchain calls to this function.
    function simulateTimedWithdraw(
        address token, uint8 tranche, uint128 minTokenOut, uint128 minQuoteOut, uint64 deadline
    ) external onlyController nonReentrant returns (uint256 tokenAmount, uint256 quoteAmount) {
        return _burnPosition(token, tranche, minTokenOut, minQuoteOut, deadline);
    }

    /// @notice Owner-triggered recovery through Q. It may withdraw at any
    /// price and sends new X/Q receipts to `recipient`. This bypasses normal
    /// boundary-exit distribution only for an explicit emergency abort.
    function emergencyUnwind(
        address token, uint128 minTokenOut, uint128 minQuoteOut, uint64 deadline, address recipient
    ) external onlyController nonReentrant returns (uint256 tokenAmount, uint256 quoteAmount) {
        if (recipient == address(0)) revert InvalidConfiguration();
        if (_activeCount(token) == 0) revert NotActive();
        for (uint8 i; i < 3; ++i) {
            if (!_position(token, i).active) continue;
            (uint256 xOut, uint256 qOut) = _burnPosition(token, i, 0, 0, deadline);
            tokenAmount += xOut;
            quoteAmount += qOut;
        }
        if (tokenAmount < minTokenOut || quoteAmount < minQuoteOut) revert WrongPosition();
        HarvestedAmounts memory harvested = harvestedAmounts[token];
        tokenAmount += harvested.tokenAmount;
        quoteAmount += harvested.quoteAmount;
        reservedHarvestedQuote -= harvested.quoteAmount;
        delete harvestedAmounts[token];
        delete exitConfigs[token];
        if (tokenAmount != 0 && !IHooklessERC20(token).transfer(recipient, tokenAmount)) {
            revert ApprovalFailed();
        }
        if (quoteAmount != 0 && !IHooklessERC20(quoteToken).transfer(recipient, quoteAmount)) {
            revert ApprovalFailed();
        }
        emit PositionRescued(token, recipient, tokenAmount, quoteAmount);
    }

    /// @notice Recover ERC20 balances already held outside the LP NFT, such
    /// as unused Q prefunding, harvested fees, or accidentally sent assets.
    /// It cannot withdraw liquidity still represented by an active position.
    function rescueHeldERC20(address token, uint256 amount, address recipient)
        external onlyController nonReentrant
    {
        if (token.code.length == 0 || recipient == address(0) || amount == 0) revert InvalidConfiguration();
        uint256 held = IHooklessERC20(token).balanceOf(address(this));
        uint256 reserved = token == quoteToken ? reservedHarvestedQuote : harvestedAmounts[token].tokenAmount;
        if (held < amount || held - amount < reserved) revert UnsettledHarvest();
        if (!IHooklessERC20(token).transfer(recipient, amount)) revert ApprovalFailed();
        emit HeldAssetRescued(token, recipient, amount);
    }

    function _burnPosition(address token, uint8 tranche, uint128 minTokenOut, uint128 minQuoteOut, uint64 deadline)
        private returns (uint256 tokenAmount, uint256 quoteAmount)
    {
        Position storage position = _position(token, tranche);
        if (!position.active) revert NotActive();
        if (deadline < block.timestamp) revert InvalidConfiguration();
        IHooklessPositionManager.PoolKey memory key = _poolKey(token, position.feePips, position.tickSpacing);
        uint256 tokenBefore = IHooklessERC20(token).balanceOf(address(this));
        uint256 quoteBefore = IHooklessERC20(quoteToken).balanceOf(address(this));
        uint128 min0 = key.currency0 == token ? minTokenOut : minQuoteOut;
        uint128 min1 = key.currency1 == token ? minTokenOut : minQuoteOut;
        bytes[] memory params = new bytes[](2);
        params[0] = abi.encode(position.tokenId, min0, min1, bytes(""));
        params[1] = abi.encode(key.currency0, key.currency1, address(this));
        positionManager.modifyLiquidities(
            abi.encode(abi.encodePacked(BURN_POSITION, TAKE_PAIR), params), uint256(deadline)
        );
        tokenAmount = IHooklessERC20(token).balanceOf(address(this)) - tokenBefore;
        quoteAmount = IHooklessERC20(quoteToken).balanceOf(address(this)) - quoteBefore;
        if (tokenAmount == 0 && quoteAmount == 0) revert NoTokensReceived();
        position.active = false;
        position.tokenWithdrawn = tokenAmount;
        position.quoteWithdrawn = quoteAmount;
        emit PositionWithdrawn(token, tokenAmount, quoteAmount);
    }

    /// @notice Burn an entered position at either live one-sided boundary and
    /// settle only that position's new and previously harvested X/Q receipts.
    /// The router liquidates X, buys/burns Q and pays configured recipients.
    /// No ETH-valued return is fabricated for fee-policy feedback yet.
    function exit(address token) external onlyController nonReentrant returns (int32 netReturnBps, bool comparable) {
        ExitConfig memory config = exitConfigs[token];
        Position storage position = _position(token, config.tranche);
        if (!position.active) revert NotActive();
        bool atQuoteBoundary;
        bool atTokenBoundary;
        if (!config.timed) {
            if (!position.enteredBand) revert NotEnteredBand();
            (, , atQuoteBoundary, atTokenBoundary,) = inspectTranche(token, config.tranche);
            if (!atQuoteBoundary && !atTokenBoundary) revert NotAtBoundary();
        }
        if (
            config.deadline < block.timestamp || config.deadline > block.timestamp + 15 minutes ||
            (atQuoteBoundary && config.minQuoteOut == 0) ||
            (atTokenBoundary && config.minTokenOut == 0)
        ) revert InvalidConfiguration();
        ExitSnapshot memory snapshot = _burnForExit(token, config);
        (uint256 ethOut, uint256 quoteBurned) = _routeExit(token, config, snapshot);
        reservedHarvestedQuote -= snapshot.harvestedQuote;
        delete harvestedAmounts[token];
        delete exitConfigs[token];
        emit PositionSettled(token, snapshot.tokenToSettle, snapshot.quoteToSettle, ethOut, quoteBurned);
        emit TrancheSettled(token, config.tranche, position.tokenId);
        return (0, false);
    }

    function _burnForExit(address token, ExitConfig memory config) private returns (ExitSnapshot memory snapshot) {
        HarvestedAmounts memory harvested = harvestedAmounts[token];
        snapshot.tokenBefore = IHooklessERC20(token).balanceOf(address(this));
        snapshot.quoteBefore = IHooklessERC20(quoteToken).balanceOf(address(this));
        snapshot.nativeBefore = address(this).balance;
        snapshot.supplyBefore = IHooklessCursorQ(quoteToken).totalSupply();
        snapshot.harvestedToken = harvested.tokenAmount;
        snapshot.harvestedQuote = harvested.quoteAmount;
        (uint256 tokenFromBurn, uint256 quoteFromBurn) =
            _burnPosition(token, config.tranche, config.minTokenOut, config.minQuoteOut, config.deadline);
        snapshot.tokenToSettle = tokenFromBurn + harvested.tokenAmount;
        snapshot.quoteToSettle = quoteFromBurn + harvested.quoteAmount;
        if (snapshot.tokenToSettle == 0 && snapshot.quoteToSettle == 0) revert NoTokensReceived();
        if (snapshot.tokenToSettle != 0 && (config.minEthOut == 0 || config.minQOut == 0)) {
            revert InvalidConfiguration();
        }
    }

    function _routeExit(address token, ExitConfig memory config, ExitSnapshot memory snapshot)
        private returns (uint256 ethOut, uint256 quoteBurned)
    {
        address router = settlementRouter;
        if (router == address(0)) revert InvalidConfiguration();
        _approveExact(token, router, snapshot.tokenToSettle);
        _approveExact(quoteToken, router, snapshot.quoteToSettle);
        (ethOut, quoteBurned) = IHooklessSettlementRouter(router).settle(
            token, snapshot.tokenToSettle, snapshot.quoteToSettle,
            snapshot.tokenToSettle == 0 ? 0 : config.minEthOut,
            snapshot.tokenToSettle == 0 ? 0 : config.minQOut,
            config.deadline
        );
        _clearApproval(token, router, snapshot.tokenToSettle);
        _clearApproval(quoteToken, router, snapshot.quoteToSettle);
        if (
            (snapshot.tokenToSettle == 0 && (ethOut != 0 || quoteBurned != snapshot.quoteToSettle)) ||
            (snapshot.tokenToSettle != 0 &&
                (ethOut < config.minEthOut || quoteBurned < snapshot.quoteToSettle + config.minQOut)) ||
            IHooklessERC20(token).balanceOf(address(this)) != snapshot.tokenBefore - snapshot.harvestedToken ||
            IHooklessERC20(quoteToken).balanceOf(address(this)) != snapshot.quoteBefore - snapshot.harvestedQuote ||
            address(this).balance != snapshot.nativeBefore ||
            IHooklessCursorQ(quoteToken).totalSupply() != snapshot.supplyBefore - quoteBurned
        ) revert WrongSettlement();
    }

    function _approveExact(address token, address router, uint256 amount) private {
        if (amount == 0) return;
        IHooklessERC20 asset = IHooklessERC20(token);
        if (!asset.approve(router, 0) || !asset.approve(router, amount)) revert ApprovalFailed();
        if (asset.allowance(address(this), router) != amount) revert ApprovalFailed();
    }

    function _clearApproval(address token, address router, uint256 amount) private {
        if (amount == 0) return;
        IHooklessERC20 asset = IHooklessERC20(token);
        if (!asset.approve(router, 0) || asset.allowance(address(this), router) != 0) revert ApprovalFailed();
    }

    function _poolKey(address token, uint24 feePips, int24 tickSpacing)
        private view returns (IHooklessPositionManager.PoolKey memory key)
    {
        bool quoteIs0 = uint160(quoteToken) < uint160(token);
        key = IHooklessPositionManager.PoolKey({
            currency0: quoteIs0 ? quoteToken : token,
            currency1: quoteIs0 ? token : quoteToken,
            fee: feePips,
            tickSpacing: tickSpacing,
            hooks: address(0)
        });
    }

    function _isQuoteOnly(address token, uint160 price, uint160 lower, uint160 upper)
        private view returns (bool)
    {
        return uint160(quoteToken) < uint160(token) ? price <= lower : price >= upper;
    }

    function _isTokenOnly(address token, uint160 price, uint160 lower, uint160 upper)
        private view returns (bool)
    {
        return uint160(quoteToken) < uint160(token) ? price >= upper : price <= lower;
    }

    function _approveQuote(uint128 maxQuoteIn) private {
        IHooklessERC20 quote = IHooklessERC20(quoteToken);
        if (quote.allowance(address(this), address(permit2)) < maxQuoteIn) {
            if (!quote.approve(address(permit2), 0)) revert ApprovalFailed();
            if (!quote.approve(address(permit2), type(uint256).max)) revert ApprovalFailed();
        }
        permit2.approve(quoteToken, address(positionManager), type(uint160).max, type(uint48).max);
    }

    function _mintTranche(
        address token,
        IHooklessPositionManager.PoolKey memory key,
        OpenConfig memory config,
        uint8 tranche
    ) private returns (uint256 quoteSpent) {
        uint256 tokenId = positionManager.nextTokenId();
        uint256 quoteBefore = IHooklessERC20(quoteToken).balanceOf(address(this));
        uint256 tokenBefore = IHooklessERC20(token).balanceOf(address(this));
        _approveQuote(config.maxQuoteIn[tranche]);
        bytes[] memory params = new bytes[](2);
        uint128 max0 = quoteToken == key.currency0 ? config.maxQuoteIn[tranche] : 0;
        uint128 max1 = quoteToken == key.currency1 ? config.maxQuoteIn[tranche] : 0;
        params[0] = abi.encode(
            key, config.tickLower[tranche], config.tickUpper[tranche],
            uint256(config.liquidity[tranche]), max0, max1, address(this), bytes("")
        );
        params[1] = abi.encode(key.currency0, key.currency1);
        positionManager.modifyLiquidities(
            abi.encode(abi.encodePacked(MINT_POSITION, SETTLE_PAIR), params), uint256(config.deadline)
        );
        if (
            positionManager.ownerOf(tokenId) != address(this) ||
            positionManager.getPositionLiquidity(tokenId) != config.liquidity[tranche]
        ) revert WrongPosition();
        uint256 quoteAfter = IHooklessERC20(quoteToken).balanceOf(address(this));
        if (
            quoteAfter < reservedHarvestedQuote || quoteAfter >= quoteBefore ||
            quoteBefore - quoteAfter > config.maxQuoteIn[tranche]
        ) revert WrongPosition();
        if (IHooklessERC20(token).balanceOf(address(this)) != tokenBefore) revert WrongPosition();
        quoteSpent = quoteBefore - quoteAfter;
    }
}
