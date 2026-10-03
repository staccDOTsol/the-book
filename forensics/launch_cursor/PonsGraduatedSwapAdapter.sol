// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

/// @dev The 15-field launch record in Pons V2's IPonsV2LaunchFactory.
interface IPonsGraduatedFactory {
    struct LaunchedToken {
        address token;
        address curve;
        address deployer;
        address creatorFeeRecipient;
        address pairToken;
        uint256 graduationThreshold;
        uint24 poolFee;
        int24 tickSpacing;
        uint16 creatorTaxBps;
        bool buybackEnabled;
        uint8 phase;
        uint256 sweptQuote;
        uint256 sweptTokens;
        uint256 sweptAt;
        bool exists;
    }

    function getLaunchedToken(address token) external view returns (LaunchedToken memory);
    function poolManager() external view returns (address);
    function memeHook() external view returns (address);
}

interface IPonsGraduatedHook {
    function factory() external view returns (address);
    function poolManager() external view returns (address);

    // The first four fields of PonsV2MemeHook.LaunchInfo. The public mapping
    // getter returns more static fields; ABI decoding safely ignores them.
    function launches(bytes32 poolId)
        external
        view
        returns (bool registered, bool memecoinIsCurrency0, address memecoin, address quoteToken);
}

interface IPonsGraduatedToken {
    function balanceOf(address account) external view returns (uint256);
    function approve(address spender, uint256 amount) external returns (bool);
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
    function transfer(address to, uint256 amount) external returns (bool);
}

/// @dev ABI-compatible with Uniswap v4-core's PoolKey and SwapParams.
struct PonsGraduatedPoolKey {
    address currency0;
    address currency1;
    uint24 fee;
    int24 tickSpacing;
    address hooks;
}

struct PonsGraduatedSwapParams {
    bool zeroForOne;
    int256 amountSpecified;
    uint160 sqrtPriceLimitX96;
}

/// @dev Exact external signatures from Uniswap v4-core's IPoolManager.
/// BalanceDelta is an int256 with amount0 in the high 128 bits and amount1
/// in the low 128 bits; the ABI return type is therefore int256 here.
interface IPonsGraduatedPoolManager {
    function unlock(bytes calldata data) external returns (bytes memory result);
    function swap(PonsGraduatedPoolKey memory key, PonsGraduatedSwapParams memory params, bytes calldata hookData)
        external
        returns (int256 swapDelta);
    function sync(address currency) external;
    function settle() external payable returns (uint256 paid);
    function take(address currency, address to, uint256 amount) external;
}

/// @notice Sells an exact amount of Pons X from one authorized source for
/// native ETH in that token's registered, graduated Pons-hook v4 pool.
/// The source must own X and approve this adapter before calling it.
contract PonsGraduatedSwapAdapter {
    uint256 private constant ROBINHOOD_CHAIN_ID = 4663;
    address private constant PONS_FACTORY = 0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e;
    address private constant PONS_HOOK = 0xE5e702641Ea86F4ae6cC3cDaeD2B886f976Be044;
    address private constant V4_POOL_MANAGER = 0x8366a39CC670B4001A1121B8F6A443A643e40951;

    // v4 TickMath.MAX_SQRT_PRICE - 1, the permissive one-for-zero limit.
    uint160 private constant MAX_SQRT_PRICE_MINUS_ONE = 1461446703485210103287273052203988822378723970341;

    IPonsGraduatedFactory public immutable factory;
    IPonsGraduatedHook public immutable hook;
    IPonsGraduatedPoolManager public immutable poolManager;
    address public immutable source;

    uint256 private _entered = 1;
    bytes32 private _expectedPoolId;
    uint256 private _expectedTokensIn;
    uint256 private _minEthOut;
    uint256 private _expectedNativeTake;

    event GraduatedPoolSold(
        address indexed token, bytes32 indexed poolId, address indexed recipient, uint256 tokensIn, uint256 ethOut
    );

    error InvalidConfiguration();
    error Unauthorized();
    error ReentrantCall();
    error Expired();
    error NotNativeGraduatedPool();
    error InvalidAmount();
    error TokenCallFailed();
    error TokenAmountMismatch();
    error InexactSwapInput();
    error InsufficientEthOutput();
    error NativeAmountMismatch();
    error NativeTransferFailed();
    error UnexpectedNativeSender();

    modifier onlySource() {
        if (msg.sender != source) revert Unauthorized();
        _;
    }

    modifier nonReentrant() {
        if (_entered != 1) revert ReentrantCall();
        _entered = 2;
        _;
        _entered = 1;
    }

    constructor(address factory_, address source_) {
        if (block.chainid != ROBINHOOD_CHAIN_ID || factory_ != PONS_FACTORY || source_ == address(0)) {
            revert InvalidConfiguration();
        }
        IPonsGraduatedFactory launchFactory = IPonsGraduatedFactory(factory_);
        address manager_ = launchFactory.poolManager();
        address hook_ = launchFactory.memeHook();
        if (manager_ != V4_POOL_MANAGER || hook_ != PONS_HOOK) revert InvalidConfiguration();
        if (IPonsGraduatedHook(hook_).factory() != factory_ || IPonsGraduatedHook(hook_).poolManager() != manager_) {
            revert InvalidConfiguration();
        }
        factory = launchFactory;
        hook = IPonsGraduatedHook(hook_);
        poolManager = IPonsGraduatedPoolManager(manager_);
        source = source_;
    }

    /// @notice Returns the pool key and ID derived from the factory's frozen
    /// launch terms, after checking Pons's hook registration.
    function registeredNativePool(address token)
        external
        view
        returns (PonsGraduatedPoolKey memory key, bytes32 poolId)
    {
        return _registeredNativePool(token);
    }

    /// @notice Pull exact X from `source`, swap all of it for native ETH, and
    /// forward only this sale's proceeds. Any incomplete fill or short payout
    /// reverts the token transfer and swap together.
    function sellNativeGraduatedPool(
        address token,
        uint256 tokensIn,
        uint256 minEthOut,
        address payable recipient,
        uint64 deadline
    ) external onlySource nonReentrant returns (uint256 ethOut) {
        if (block.timestamp > deadline) revert Expired();
        if (tokensIn == 0 || tokensIn > uint256(uint128(type(int128).max)) || minEthOut == 0 || recipient == address(0))
        {
            revert InvalidAmount();
        }

        (PonsGraduatedPoolKey memory key, bytes32 poolId) = _registeredNativePool(token);
        uint256 tokenBefore = _pullExact(token, tokensIn);
        ethOut = _swapExact(key, poolId, tokensIn, minEthOut);
        if (IPonsGraduatedToken(token).balanceOf(address(this)) != tokenBefore) revert TokenAmountMismatch();
        (bool sent,) = recipient.call{value: ethOut}("");
        if (!sent) revert NativeTransferFailed();
        emit GraduatedPoolSold(token, poolId, recipient, tokensIn, ethOut);
    }

    /// @dev Called only by the official v4 PoolManager during our own unlock.
    function unlockCallback(bytes calldata data) external returns (bytes memory) {
        if (msg.sender != address(poolManager) || _entered != 2 || _expectedTokensIn == 0) revert Unauthorized();
        (PonsGraduatedPoolKey memory key, uint256 tokensIn) = abi.decode(data, (PonsGraduatedPoolKey, uint256));
        if (keccak256(abi.encode(key)) != _expectedPoolId || tokensIn != _expectedTokensIn) revert Unauthorized();

        // Native ETH is always currency0, so selling X is one-for-zero.
        int256 packedDelta = poolManager.swap(
            key,
            PonsGraduatedSwapParams({
                zeroForOne: false, amountSpecified: -int256(tokensIn), sqrtPriceLimitX96: MAX_SQRT_PRICE_MINUS_ONE
            }),
            ""
        );
        int128 ethDelta = int128(packedDelta >> 128);
        int128 tokenDelta = int128(packedDelta);
        if (int256(tokenDelta) != -int256(tokensIn)) revert InexactSwapInput();
        if (ethDelta <= 0 || uint256(uint128(ethDelta)) < _minEthOut) revert InsufficientEthOutput();
        uint256 ethOut = uint256(uint128(ethDelta));

        // sync precedes the transfer per v4 flash accounting. Check both the
        // token's actual transfer and PoolManager's credited settlement.
        uint256 managerBefore = IPonsGraduatedToken(key.currency1).balanceOf(address(poolManager));
        poolManager.sync(key.currency1);
        _safeTransfer(key.currency1, address(poolManager), tokensIn);
        if (IPonsGraduatedToken(key.currency1).balanceOf(address(poolManager)) - managerBefore != tokensIn) {
            revert TokenAmountMismatch();
        }
        if (poolManager.settle() != tokensIn) revert TokenAmountMismatch();

        uint256 nativeBefore = address(this).balance;
        _expectedNativeTake = ethOut;
        poolManager.take(address(0), address(this), ethOut);
        _expectedNativeTake = 0;
        if (address(this).balance - nativeBefore != ethOut) revert NativeAmountMismatch();
        return abi.encode(ethOut);
    }

    receive() external payable {
        if (msg.sender != address(poolManager) || _entered != 2 || msg.value != _expectedNativeTake) {
            revert UnexpectedNativeSender();
        }
    }

    function _registeredNativePool(address token)
        private
        view
        returns (PonsGraduatedPoolKey memory key, bytes32 poolId)
    {
        if (token == address(0) || token.code.length == 0) {
            revert NotNativeGraduatedPool();
        }
        IPonsGraduatedFactory.LaunchedToken memory launch = factory.getLaunchedToken(token);
        if (
            !launch.exists || launch.token != token || launch.curve.code.length == 0 || launch.pairToken != address(0)
                || launch.phase != 2 || launch.tickSpacing <= 0
        ) revert NotNativeGraduatedPool();

        key = PonsGraduatedPoolKey({
            currency0: address(0),
            currency1: token,
            fee: launch.poolFee,
            tickSpacing: launch.tickSpacing,
            hooks: address(hook)
        });
        poolId = keccak256(abi.encode(key));
        (bool registered, bool memecoinIsCurrency0, address memecoin, address quoteToken) = hook.launches(poolId);
        if (!registered || memecoinIsCurrency0 || memecoin != token || quoteToken != address(0)) {
            revert NotNativeGraduatedPool();
        }
    }

    function _pullExact(address token, uint256 tokensIn) private returns (uint256 tokenBefore) {
        IPonsGraduatedToken x = IPonsGraduatedToken(token);
        uint256 sourceBefore = x.balanceOf(source);
        tokenBefore = x.balanceOf(address(this));
        _safeTransferFrom(token, source, address(this), tokensIn);
        uint256 sourceAfter = x.balanceOf(source);
        if (sourceAfter > sourceBefore || sourceBefore - sourceAfter != tokensIn) revert TokenAmountMismatch();
        if (x.balanceOf(address(this)) - tokenBefore != tokensIn) revert TokenAmountMismatch();
    }

    function _swapExact(PonsGraduatedPoolKey memory key, bytes32 poolId, uint256 tokensIn, uint256 minEthOut)
        private
        returns (uint256 ethOut)
    {
        uint256 nativeBefore = address(this).balance;
        _expectedPoolId = poolId;
        _expectedTokensIn = tokensIn;
        _minEthOut = minEthOut;
        uint256 reported = abi.decode(poolManager.unlock(abi.encode(key, tokensIn)), (uint256));
        _expectedPoolId = bytes32(0);
        _expectedTokensIn = 0;
        _minEthOut = 0;
        ethOut = address(this).balance - nativeBefore;
        if (ethOut != reported || ethOut < minEthOut) revert NativeAmountMismatch();
    }

    function _safeTransferFrom(address token, address from, address to, uint256 amount) private {
        (bool ok, bytes memory result) =
            token.call(abi.encodeWithSelector(IPonsGraduatedToken.transferFrom.selector, from, to, amount));
        if (!ok || (result.length != 0 && (result.length != 32 || !abi.decode(result, (bool))))) {
            revert TokenCallFailed();
        }
    }

    function _safeTransfer(address token, address to, uint256 amount) private {
        (bool ok, bytes memory result) =
            token.call(abi.encodeWithSelector(IPonsGraduatedToken.transfer.selector, to, amount));
        if (!ok || (result.length != 0 && (result.length != 32 || !abi.decode(result, (bool))))) {
            revert TokenCallFailed();
        }
    }
}
