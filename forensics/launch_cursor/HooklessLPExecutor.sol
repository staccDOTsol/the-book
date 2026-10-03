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
}

interface IHooklessCursorQ {
    function executor() external view returns (address);
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
/// provide an independently checked executable starting price and funding.
/// This contract does not sell X, burn Q, pay recipients, or compute ROI.
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
    address public immutable poolManager;
    IHooklessPositionManager public immutable positionManager;
    IHooklessStateView public immutable stateView;
    IHooklessPermit2 public immutable permit2;

    mapping(address token => OpenConfig) public openConfigs;
    mapping(address token => Position) public positions;

    uint256 private _locked = 1;

    event ControllerBound(address indexed controller);
    event OpenConfigured(address indexed token, uint160 startingSqrtPriceX96, int24 tickLower, int24 tickUpper);
    event PositionOpened(address indexed token, bytes32 indexed poolId, uint256 indexed tokenId, uint24 feePips, uint256 quoteSpent);
    event BandEntered(address indexed token, uint256 indexed tokenId);
    event FeesCollected(address indexed token, uint256 tokenAmount, uint256 quoteAmount);
    event PositionWithdrawn(address indexed token, uint256 tokenAmount, uint256 quoteAmount);

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

    /// @notice The controller must commit bounded price, range and spend data
    /// before open. Its upstream quote must be executable and independently
    /// checked; this contract cannot infer a fair X/Q price from its own empty
    /// pool. A later retry uses this same configuration until the controller
    /// changes it, but the fee is supplied separately by the controller.
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

    /// @notice Creates a new zero-hook, static-fee pool and Q-only position in
    /// the same PositionManager multicall. `feePips` is the controller's frozen
    /// selection for this token (50,000 = 5%; 500,000 = 50%).
    function open(address token, uint24 feePips) external onlyController nonReentrant {
        Position storage position = positions[token];
        if (position.active || position.tokenId != 0) revert AlreadyOpened();
        OpenConfig memory config = openConfigs[token];
        if (
            config.liquidity == 0 || config.deadline < block.timestamp ||
            feePips < 50_000 || feePips > 500_000 || quoteToken.code.length == 0
        ) revert InvalidConfiguration();

        IHooklessPositionManager.PoolKey memory key = _poolKey(token, feePips, config.tickSpacing);
        bytes32 poolId = keccak256(abi.encode(key));
        (uint160 beforePrice,,,) = stateView.getSlot0(poolId);
        if (beforePrice != 0) revert AlreadyInitialized();

        uint160 lower = config.tickLower.getSqrtPriceAtTick();
        uint160 upper = config.tickUpper.getSqrtPriceAtTick();
        if (!_isQuoteOnly(token, config.startingSqrtPriceX96, lower, upper)) revert InvalidConfiguration();

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
        if (deadline < block.timestamp) revert InvalidConfiguration();
        (, , bool atQuoteBoundary, bool atTokenBoundary,) = inspect(token);
        if (!atQuoteBoundary && !atTokenBoundary) revert NotAtBoundary();
        if ((atQuoteBoundary && minQuoteOut == 0) || (atTokenBoundary && minTokenOut == 0)) {
            revert InvalidConfiguration();
        }
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

    /// @dev The cursor needs a realized ETH-valued result for fee learning.
    /// Burning the NFT alone cannot produce that figure. A complete exit must
    /// liquidate X, burn Q, pay WETH/ETH, then calculate and return net ROI.
    function exit(address) external view onlyController returns (int32) {
        revert OutcomeUnavailable();
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
        if (quoteBefore < config.maxQuoteIn) revert InvalidConfiguration();
        _approveQuote(config.maxQuoteIn);
        _positionMulticall(key, config);

        (uint160 afterPrice,,, uint24 liveFee) = stateView.getSlot0(poolId);
        if (afterPrice != config.startingSqrtPriceX96 || liveFee != key.fee) revert WrongPoolState();
        if (
            positionManager.ownerOf(tokenId) != address(this) ||
            positionManager.getPositionLiquidity(tokenId) != config.liquidity
        ) revert WrongPosition();
        uint256 quoteAfter = IHooklessERC20(quoteToken).balanceOf(address(this));
        if (quoteAfter >= quoteBefore || quoteBefore - quoteAfter > config.maxQuoteIn) revert WrongPosition();
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
