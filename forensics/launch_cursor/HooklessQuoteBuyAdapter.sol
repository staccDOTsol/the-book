// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

/// @dev ABI-compatible with Uniswap v4-core's PoolKey and SwapParams.
struct HooklessQuoteBuyPoolKey {
    address currency0;
    address currency1;
    uint24 fee;
    int24 tickSpacing;
    address hooks;
}

struct HooklessQuoteBuySwapParams {
    bool zeroForOne;
    int256 amountSpecified;
    uint160 sqrtPriceLimitX96;
}

/// @dev BalanceDelta is packed as amount0 in the high 128 bits and amount1
/// in the low 128 bits, so its external ABI is an int256.
interface IHooklessQuoteBuyPoolManager {
    function unlock(bytes calldata data) external returns (bytes memory result);
    function swap(HooklessQuoteBuyPoolKey memory key, HooklessQuoteBuySwapParams memory params, bytes calldata hookData)
        external
        returns (int256 swapDelta);
    function settle() external payable returns (uint256 paid);
    function take(address currency, address to, uint256 amount) external;
}

interface IHooklessQuoteBuyToken {
    function balanceOf(address account) external view returns (uint256);
    function transfer(address to, uint256 amount) external returns (bool);
}

/// @notice Converts exact native ETH from one authorized source into Q through
/// its known, zero-hook Uniswap v4 Q/ETH pool on Robinhood.
/// @dev The caller supplies the launch pool's immutable fee and tick spacing
/// at deployment. Q must be a standard, non-rebasing ERC-20 for exact receipt.
contract HooklessQuoteBuyAdapter {
    uint256 private constant ROBINHOOD_CHAIN_ID = 4663;
    address private constant V4_POOL_MANAGER = 0x8366a39CC670B4001A1121B8F6A443A643e40951;
    // v4 TickMath.MIN_SQRT_PRICE + 1: permissive zero-for-one limit.
    uint160 private constant MIN_SQRT_PRICE_PLUS_ONE = 4295128740;

    IHooklessQuoteBuyPoolManager public immutable poolManager;
    IHooklessQuoteBuyToken public immutable quoteToken;
    address public immutable source;
    uint24 public immutable fee;
    int24 public immutable tickSpacing;
    bytes32 public immutable poolId;

    uint256 private _entered = 1;
    uint256 private _expectedEthIn;
    uint256 private _minQOut;

    event QuoteBought(address indexed recipient, bytes32 indexed poolId, uint256 ethIn, uint256 qOut);

    error InvalidConfiguration();
    error Unauthorized();
    error ReentrantCall();
    error Expired();
    error InvalidAmount();
    error InexactSwapInput();
    error InsufficientQOutput();
    error NativeAmountMismatch();
    error QuoteAmountMismatch();
    error QuoteTransferFailed();
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

    constructor(address poolManager_, address quoteToken_, uint24 fee_, int24 tickSpacing_, address source_) {
        if (
            block.chainid != ROBINHOOD_CHAIN_ID || poolManager_ != V4_POOL_MANAGER || poolManager_.code.length == 0
                || quoteToken_ == address(0) || quoteToken_.code.length == 0 || source_ == address(0)
                || fee_ > 1_000_000 || tickSpacing_ <= 0 || tickSpacing_ > type(int16).max
        ) revert InvalidConfiguration();

        poolManager = IHooklessQuoteBuyPoolManager(poolManager_);
        quoteToken = IHooklessQuoteBuyToken(quoteToken_);
        source = source_;
        fee = fee_;
        tickSpacing = tickSpacing_;
        poolId = keccak256(abi.encode(address(0), quoteToken_, fee_, tickSpacing_, address(0)));
    }

    function poolKey() public view returns (HooklessQuoteBuyPoolKey memory) {
        return HooklessQuoteBuyPoolKey({
            currency0: address(0), currency1: address(quoteToken), fee: fee, tickSpacing: tickSpacing, hooks: address(0)
        });
    }

    /// @notice Spend all `msg.value` on Q and deliver this swap's exact output
    /// to `recipient`. A partial v4 fill, taxed Q transfer, or short output
    /// reverts the entire swap and native settlement.
    function buyQ(uint256 minQOut, address recipient, uint64 deadline)
        external
        payable
        onlySource
        nonReentrant
        returns (uint256 qOut)
    {
        if (block.timestamp > deadline) revert Expired();
        uint256 ethIn = msg.value;
        if (
            ethIn == 0 || ethIn > uint256(uint128(type(int128).max)) || minQOut == 0 || recipient == address(0)
                || recipient == address(this)
        ) revert InvalidAmount();

        uint256 nativeBefore = address(this).balance - ethIn;
        uint256 quoteBefore = quoteToken.balanceOf(address(this));
        _expectedEthIn = ethIn;
        _minQOut = minQOut;
        uint256 reported = abi.decode(poolManager.unlock(abi.encode(ethIn)), (uint256));
        _expectedEthIn = 0;
        _minQOut = 0;
        if (address(this).balance != nativeBefore) revert NativeAmountMismatch();

        qOut = quoteToken.balanceOf(address(this)) - quoteBefore;
        if (qOut != reported || qOut < minQOut) revert QuoteAmountMismatch();
        uint256 recipientBefore = quoteToken.balanceOf(recipient);
        _safeTransfer(recipient, qOut);
        if (
            quoteToken.balanceOf(address(this)) != quoteBefore
                || quoteToken.balanceOf(recipient) - recipientBefore != qOut
        ) revert QuoteAmountMismatch();
        emit QuoteBought(recipient, poolId, ethIn, qOut);
    }

    /// @dev Only the official PoolManager can call this during our own unlock.
    function unlockCallback(bytes calldata data) external returns (bytes memory) {
        uint256 ethIn = _expectedEthIn;
        if (
            msg.sender != address(poolManager) || _entered != 2 || ethIn == 0 || data.length != 32
                || abi.decode(data, (uint256)) != ethIn
        ) revert Unauthorized();

        int256 packedDelta = poolManager.swap(
            poolKey(),
            HooklessQuoteBuySwapParams({
                zeroForOne: true, amountSpecified: -int256(ethIn), sqrtPriceLimitX96: MIN_SQRT_PRICE_PLUS_ONE
            }),
            ""
        );
        int128 ethDelta = int128(packedDelta >> 128);
        int128 quoteDelta = int128(packedDelta);
        if (int256(ethDelta) != -int256(ethIn)) revert InexactSwapInput();
        if (quoteDelta <= 0 || uint256(uint128(quoteDelta)) < _minQOut) revert InsufficientQOutput();
        uint256 qOut = uint256(uint128(quoteDelta));

        uint256 nativeBefore = address(this).balance;
        if (poolManager.settle{value: ethIn}() != ethIn || address(this).balance != nativeBefore - ethIn) {
            revert NativeAmountMismatch();
        }

        uint256 quoteBefore = quoteToken.balanceOf(address(this));
        poolManager.take(address(quoteToken), address(this), qOut);
        if (quoteToken.balanceOf(address(this)) - quoteBefore != qOut) revert QuoteAmountMismatch();
        return abi.encode(qOut);
    }

    receive() external payable {
        revert UnexpectedNativeSender();
    }

    function _safeTransfer(address to, uint256 amount) private {
        (bool ok, bytes memory result) =
            address(quoteToken).call(abi.encodeWithSelector(IHooklessQuoteBuyToken.transfer.selector, to, amount));
        if (!ok || (result.length != 0 && (result.length != 32 || !abi.decode(result, (bool))))) {
            revert QuoteTransferFailed();
        }
    }
}
