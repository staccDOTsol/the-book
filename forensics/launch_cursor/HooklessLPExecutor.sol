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

/// @notice Prototype custodian for one hookless, statically priced X/Q pool
/// and one Q-funded v4 position per Pons launch token X. The controller must
/// provide bounded funding. Live spot and full-inventory executable depth
/// are checked before mint. Boundary exits use the bound settlement router;
/// ETH-valued return accounting remains separate integration work.
contract HooklessLPExecutor {
    using HooklessTickMath for int24;

    uint8 private constant MINT_POSITION = 0x02;
    uint8 private constant DECREASE_LIQUIDITY = 0x01;
    uint8 private constant BURN_POSITION = 0x03;
    uint8 private constant SETTLE_PAIR = 0x0d;
    uint8 private constant TAKE_PAIR = 0x11;

    struct OpenConfig {
        uint160 startingSqrtPriceX96;
        uint128 liquidity;
        uint128 maxQuoteIn;
        int24 tickSpacing;
        int24 tickLower;
        int24 tickUpper;
        uint64 deadline;
    }

    struct ExitConfig {
        uint128 minTokenOut;
        uint128 minQuoteOut;
        uint256 minEthOut;
        uint256 minQOut;
        uint64 deadline;
    }

    struct HarvestedAmounts {
        uint256 tokenAmount;
        uint256 quoteAmount;
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
    address public priceGuard;
    address public depthGuard;
    address public settlementRouter;
    address public immutable poolManager;
    IHooklessPositionManager public immutable positionManager;
    IHooklessStateView public immutable stateView;
    IHooklessPermit2 public immutable permit2;

    mapping(address token => OpenConfig) public openConfigs;
    mapping(address token => ExitConfig) public exitConfigs;
    mapping(address token => HarvestedAmounts) public harvestedAmounts;
    mapping(address token => Position) public positions;
    uint256 public reservedHarvestedQuote;

    uint256 private _locked = 1;

    event ControllerBound(address indexed controller);
    event PriceGuardBound(address indexed guard);
    event DepthGuardBound(address indexed guard);
    event SettlementRouterBound(address indexed router);
    event OpenConfigured(address indexed token, uint160 startingSqrtPriceX96, int24 tickLower, int24 tickUpper);
    event ExitConfigured(
        address indexed token, uint128 minTokenOut, uint128 minQuoteOut,
        uint256 minEthOut, uint256 minQOut, uint64 deadline
    );
    event PositionOpened(address indexed token, bytes32 indexed poolId, uint256 indexed tokenId, uint24 feePips, uint256 quoteSpent);
    event BandEntered(address indexed token, uint256 indexed tokenId);
    event FeesCollected(address indexed token, uint256 tokenAmount, uint256 quoteAmount);
    event PositionWithdrawn(address indexed token, uint256 tokenAmount, uint256 quoteAmount);
    event PositionSettled(address indexed token, uint256 tokenSettled, uint256 quoteSettled, uint256 ethOut, uint256 quoteBurned);
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

    /// @notice Bind the Q/ETH + Pons cross-price guard after Q and its launch
    /// pool exist. Opening remains disabled until this reciprocal check passes.
    function bindPriceGuard(address guard) external nonReentrant {
        if (msg.sender != owner) revert NotOwner();
        if (
            priceGuard != address(0) || controller == address(0) || guard.code.length == 0 ||
            IHooklessPriceGuard(guard).quoteToken() != quoteToken ||
            IHooklessPriceGuard(guard).stateView() != address(stateView) ||
            IHooklessPriceGuard(guard).ponsFactory() != IHooklessCursorQ(controller).ponsFactory()
        ) revert InvalidConfiguration();
        priceGuard = guard;
        emit PriceGuardBound(guard);
    }

    /// @notice One-time bind after Q and its launch pool exist. The router is
    /// deployed for this executor and the same Q/ETH pool as the open guard.
    function bindSettlementRouter(address router) external nonReentrant {
        if (msg.sender != owner) revert NotOwner();
        if (settlementRouter != address(0) || priceGuard == address(0) || router.code.length == 0) {
            revert InvalidConfiguration();
        }
        IHooklessSettlementRouter route = IHooklessSettlementRouter(router);
        if (
            route.source() != address(this) || route.quoteToken() != quoteToken ||
            route.factory() != IHooklessCursorQ(controller).ponsFactory() ||
            route.weth().code.length == 0 || route.wizardFanout().code.length == 0 ||
            route.developer() == address(0)
        ) revert InvalidConfiguration();
        address buyer = route.quoteBuy();
        if (buyer.code.length == 0) revert InvalidConfiguration();
        IHooklessExitQuoteBuy quoteBuyer = IHooklessExitQuoteBuy(buyer);
        if (
            quoteBuyer.source() != router || quoteBuyer.poolManager() != poolManager ||
            quoteBuyer.quoteToken() != quoteToken ||
            quoteBuyer.poolId() != IHooklessPriceGuard(priceGuard).quoteEthPoolId()
        ) revert InvalidConfiguration();
        address activeSale = route.activeSale();
        address graduatedSale = route.graduatedSale();
        if (activeSale.code.length == 0 || graduatedSale.code.length == 0) revert InvalidConfiguration();
        if (
            IHooklessExitSaleAdapter(activeSale).source() != router ||
            IHooklessExitSaleAdapter(graduatedSale).source() != router ||
            IHooklessExitSaleAdapter(activeSale).factory() != route.factory() ||
            IHooklessExitSaleAdapter(graduatedSale).factory() != route.factory()
        ) revert InvalidConfiguration();
        settlementRouter = router;
        emit SettlementRouterBound(router);
    }

    /// @notice Bind the executable full-inventory quote guard after the spot
    /// guard and settlement router. The guard constructor verifies their Q,
    /// Pons, and Quoter identities; this binding verifies its source trio.
    function bindDepthGuard(address guard) external nonReentrant {
        if (msg.sender != owner) revert NotOwner();
        if (
            depthGuard != address(0) || priceGuard == address(0) ||
            settlementRouter == address(0) || guard.code.length == 0
        ) revert InvalidConfiguration();
        if (!IHooklessExecutableDepthGuard(guard).matches(address(this), priceGuard, settlementRouter)) {
            revert InvalidConfiguration();
        }
        depthGuard = guard;
        emit DepthGuardBound(guard);
    }

    /// @notice The controller must commit bounded price, range and spend data
    /// before open. Live executable depth is checked again inside open. A
    /// later retry uses this same configuration until the controller changes
    /// it, but the fee is supplied separately by the controller.
    function configureOpen(address token, OpenConfig calldata config) external onlyController nonReentrant {
        if (
            token == address(0) || token == quoteToken || token.code.length == 0 ||
            positions[token].tokenId != 0
        ) {
            revert InvalidConfiguration();
        }
        if (
            config.liquidity == 0 || config.maxQuoteIn == 0 || config.deadline < block.timestamp ||
            config.tickSpacing < 1 || config.tickSpacing > type(int16).max ||
            config.tickLower < HooklessTickMath.MIN_TICK ||
            config.tickUpper > HooklessTickMath.MAX_TICK ||
            config.startingSqrtPriceX96 < HooklessTickMath.MIN_SQRT_PRICE ||
            config.startingSqrtPriceX96 >= HooklessTickMath.MAX_SQRT_PRICE ||
            config.tickLower >= config.tickUpper ||
            config.tickLower % config.tickSpacing != 0 ||
            config.tickUpper % config.tickSpacing != 0
        ) revert InvalidConfiguration();
        uint160 lower = config.tickLower.getSqrtPriceAtTick();
        uint160 upper = config.tickUpper.getSqrtPriceAtTick();
        if (!_isQuoteOnly(token, config.startingSqrtPriceX96, lower, upper)) revert InvalidConfiguration();
        openConfigs[token] = config;
        emit OpenConfigured(token, config.startingSqrtPriceX96, config.tickLower, config.tickUpper);
    }

    /// @notice Commit short-lived burn and swap minima. The controller only
    /// forwards this after the inspector has queued a boundary exit.
    function configureExit(address token, ExitConfig calldata config) external onlyController nonReentrant {
        Position storage position = positions[token];
        if (
            !position.active || !position.enteredBand || settlementRouter == address(0) ||
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

    /// @notice Creates a new zero-hook, static-fee pool and Q-only position in
    /// the same PositionManager multicall. `feePips` is the controller's frozen
    /// selection for this token (50,000 = 5%; 500,000 = 50%).
    function open(address token, uint24 feePips) external onlyController nonReentrant {
        Position storage position = positions[token];
        if (position.active || position.tokenId != 0) revert AlreadyOpened();
        OpenConfig memory config = openConfigs[token];
        if (
            config.liquidity == 0 || config.deadline < block.timestamp ||
            feePips < 50_000 || feePips > 500_000 || quoteToken.code.length == 0 ||
            priceGuard == address(0) || depthGuard == address(0) || settlementRouter == address(0)
        ) revert InvalidConfiguration();

        // The price keeper's saved config may be stale by the time a Q
        // transfer processes it. Check live source prices in this same tx.
        IHooklessPriceGuard(priceGuard).validate(token, config.startingSqrtPriceX96);

        IHooklessPositionManager.PoolKey memory key = _poolKey(token, feePips, config.tickSpacing);
        bytes32 poolId = keccak256(abi.encode(key));
        (uint160 beforePrice,,,) = stateView.getSlot0(poolId);
        if (beforePrice != 0) revert AlreadyInitialized();

        uint160 lower = config.tickLower.getSqrtPriceAtTick();
        uint160 upper = config.tickUpper.getSqrtPriceAtTick();
        if (!_isQuoteOnly(token, config.startingSqrtPriceX96, lower, upper)) revert InvalidConfiguration();

        // Quoter swaps are simulated by a reversible PoolManager unlock. This
        // must be a non-view call in the same transaction as initialization.
        IHooklessExecutableDepthGuard(depthGuard).validate(
            token, config.liquidity, config.tickLower, config.tickUpper
        );

        (uint256 tokenId, uint256 quoteSpent) = _initializeAndMint(token, key, config, poolId);

        position.tokenId = tokenId;
        position.poolId = poolId;
        position.feePips = feePips;
        position.tickSpacing = config.tickSpacing;
        position.tickLower = config.tickLower;
        position.tickUpper = config.tickUpper;
        position.active = true;
        position.quoteSpent = quoteSpent;
        delete openConfigs[token];
        emit PositionOpened(token, poolId, tokenId, feePips, position.quoteSpent);
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
        Position storage position = positions[token];
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
        Position storage position = positions[token];
        if (!position.active) revert NotActive();
        if (position.enteredBand) return;
        (, bool inBand,, bool atTokenBoundary,) = inspect(token);
        if (!inBand && !atTokenBoundary) revert NotInBand();
        position.enteredBand = true;
        emit BandEntered(token, position.tokenId);
    }

    /// @notice Collects X and Q swap fees while leaving principal liquidity.
    /// `previewHarvest` deliberately reverts until executable ETH valuation and
    /// gas estimation exist, so the cursor will not schedule this by mistake.
    function harvest(address token) external onlyController nonReentrant {
        Position storage position = positions[token];
        if (!position.active) revert NotActive();
        IHooklessPositionManager.PoolKey memory key = _poolKey(token, position.feePips, position.tickSpacing);
        uint256 tokenBefore = IHooklessERC20(token).balanceOf(address(this));
        uint256 quoteBefore = IHooklessERC20(quoteToken).balanceOf(address(this));
        bytes[] memory params = new bytes[](2);
        params[0] = abi.encode(position.tokenId, uint256(0), uint128(0), uint128(0), bytes(""));
        params[1] = abi.encode(key.currency0, key.currency1, address(this));
        positionManager.modifyLiquidities(
            abi.encode(abi.encodePacked(DECREASE_LIQUIDITY, TAKE_PAIR), params), block.timestamp
        );
        uint256 tokenAmount = IHooklessERC20(token).balanceOf(address(this)) - tokenBefore;
        uint256 quoteAmount = IHooklessERC20(quoteToken).balanceOf(address(this)) - quoteBefore;
        if (tokenAmount == 0 && quoteAmount == 0) revert NoTokensReceived();
        harvestedAmounts[token].tokenAmount += tokenAmount;
        harvestedAmounts[token].quoteAmount += quoteAmount;
        reservedHarvestedQuote += quoteAmount;
        emit FeesCollected(token, tokenAmount, quoteAmount);
    }

    function previewHarvest(address) external pure returns (uint256, uint256) {
        revert OutcomeUnavailable();
    }

    /// @notice Mechanically withdraws and collects everything from a position.
    /// The proceeds remain in this vault for a later liquidation/burn/payout
    /// implementation. Minima apply to principal; fee receipts can add more.
    function withdrawPosition(address token, uint128 minTokenOut, uint128 minQuoteOut, uint64 deadline)
        external onlyController nonReentrant returns (uint256 tokenAmount, uint256 quoteAmount)
    {
        Position storage position = positions[token];
        if (!position.active) revert NotActive();
        if (!position.enteredBand) revert NotEnteredBand();
        (, , bool atQuoteBoundary, bool atTokenBoundary,) = inspect(token);
        if (!atQuoteBoundary && !atTokenBoundary) revert NotAtBoundary();
        if ((atQuoteBoundary && minQuoteOut == 0) || (atTokenBoundary && minTokenOut == 0)) {
            revert InvalidConfiguration();
        }
        return _burnPosition(token, minTokenOut, minQuoteOut, deadline);
    }

    /// @notice Owner-triggered recovery through Q. It may withdraw at any
    /// price and sends new X/Q receipts to `recipient`. This bypasses normal
    /// boundary-exit distribution only for an explicit emergency abort.
    function emergencyUnwind(
        address token, uint128 minTokenOut, uint128 minQuoteOut, uint64 deadline, address recipient
    ) external onlyController nonReentrant returns (uint256 tokenAmount, uint256 quoteAmount) {
        if (recipient == address(0)) revert InvalidConfiguration();
        (tokenAmount, quoteAmount) = _burnPosition(token, minTokenOut, minQuoteOut, deadline);
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

    function _burnPosition(address token, uint128 minTokenOut, uint128 minQuoteOut, uint64 deadline)
        private returns (uint256 tokenAmount, uint256 quoteAmount)
    {
        Position storage position = positions[token];
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
        Position storage position = positions[token];
        if (!position.active) revert NotActive();
        if (!position.enteredBand) revert NotEnteredBand();
        (, , bool atQuoteBoundary, bool atTokenBoundary,) = inspect(token);
        if (!atQuoteBoundary && !atTokenBoundary) revert NotAtBoundary();
        ExitConfig memory config = exitConfigs[token];
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
            _burnPosition(token, config.minTokenOut, config.minQuoteOut, config.deadline);
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

    function _initializeAndMint(
        address token,
        IHooklessPositionManager.PoolKey memory key,
        OpenConfig memory config,
        bytes32 poolId
    ) private returns (uint256 tokenId, uint256 quoteSpent) {
        tokenId = positionManager.nextTokenId();
        uint256 quoteBefore = IHooklessERC20(quoteToken).balanceOf(address(this));
        uint256 tokenBefore = IHooklessERC20(token).balanceOf(address(this));
        uint256 reserved = reservedHarvestedQuote;
        if (quoteBefore < reserved || quoteBefore - reserved < config.maxQuoteIn) revert InvalidConfiguration();
        _approveQuote(config.maxQuoteIn);
        _positionMulticall(key, config);

        (uint160 afterPrice,,, uint24 liveFee) = stateView.getSlot0(poolId);
        if (afterPrice != config.startingSqrtPriceX96 || liveFee != key.fee) revert WrongPoolState();
        if (
            positionManager.ownerOf(tokenId) != address(this) ||
            positionManager.getPositionLiquidity(tokenId) != config.liquidity
        ) revert WrongPosition();
        uint256 quoteAfter = IHooklessERC20(quoteToken).balanceOf(address(this));
        if (
            quoteAfter < reserved || quoteAfter >= quoteBefore ||
            quoteBefore - quoteAfter > config.maxQuoteIn
        ) revert WrongPosition();
        if (IHooklessERC20(token).balanceOf(address(this)) != tokenBefore) revert WrongPosition();
        quoteSpent = quoteBefore - quoteAfter;
    }

    function _positionMulticall(IHooklessPositionManager.PoolKey memory key, OpenConfig memory config) private {
        bytes[] memory calls = new bytes[](2);
        calls[0] = abi.encodeWithSelector(
            IHooklessPositionManager.initializePool.selector, key, config.startingSqrtPriceX96
        );
        bytes[] memory params = new bytes[](2);
        uint128 max0 = quoteToken == key.currency0 ? config.maxQuoteIn : 0;
        uint128 max1 = quoteToken == key.currency1 ? config.maxQuoteIn : 0;
        params[0] = abi.encode(
            key, config.tickLower, config.tickUpper, uint256(config.liquidity),
            max0, max1, address(this), bytes("")
        );
        params[1] = abi.encode(key.currency0, key.currency1);
        calls[1] = abi.encodeWithSelector(
            IHooklessPositionManager.modifyLiquidities.selector,
            abi.encode(abi.encodePacked(MINT_POSITION, SETTLE_PAIR), params), uint256(config.deadline)
        );
        positionManager.multicall(calls);
    }
}
