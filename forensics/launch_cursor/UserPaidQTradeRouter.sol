// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {
    HooklessQuoteBuyPoolKey,
    HooklessQuoteBuySwapParams,
    IHooklessQuoteBuyPoolManager
} from "./HooklessQuoteBuyAdapter.sol";

interface IUserPaidQPoolManager is IHooklessQuoteBuyPoolManager {
    function sync(address currency) external;
}

interface IUserPaidQCursor {
    function balanceOf(address account) external view returns (uint256);
    function transfer(address to, uint256 amount) external returns (bool);
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
    function processNext() external returns (bool attempted, bool succeeded);
    function transferStepGasLimit() external view returns (uint32);
}

/// @notice Opt-in exact-input Q/ETH route for the already deployed Q. Each
/// successful route asks Q to process one queued action after v4 unlock and
/// settlement. Only calls through this router have this property; a trader
/// can still use any other router or the PoolManager directly.
/// @dev Unreviewed prototype. The immutable Q/ETH key is Robinhood's existing
/// zero-hook pool, not a new venue. A missing, expired, or failing cursor
/// action reverts the entire trade and its swap.
contract UserPaidQTradeRouter {
    uint256 private constant ROBINHOOD_CHAIN_ID = 4663;
    address private constant V4_POOL_MANAGER = 0x8366a39CC670B4001A1121B8F6A443A643e40951;
    address private constant LIVE_Q = 0x0E34d0792032Ffc54C058751cF048dA347193472;
    uint24 private constant Q_ETH_FEE = 2_500;
    int24 private constant Q_ETH_TICK_SPACING = 25;
    uint160 private constant MIN_SQRT_PRICE_PLUS_ONE = 4295128740;
    uint160 private constant MAX_SQRT_PRICE_MINUS_ONE = 1461446703485210103287273052203988822378723970341;

    IUserPaidQPoolManager public immutable poolManager;
    IUserPaidQCursor public immutable quoteToken;
    bytes32 public immutable poolId;

    uint256 private _entered = 1;
    bytes32 private _callbackHash;

    event Bought(address indexed buyer, address indexed recipient, uint256 ethIn, uint256 qOut);
    event Sold(address indexed seller, address indexed recipient, uint256 qIn, uint256 ethOut);
    event CursorProcessed(address indexed payer, bool attempted, bool succeeded);

    error WrongChainOrDeployment();
    error ReentrantCall();
    error UnauthorizedCallback();
    error Expired();
    error InvalidTrade();
    error PartialFill();
    error InsufficientOutput();
    error WrongSettlement();
    error TokenTransferFailed();
    error NativeTransferFailed();
    error CursorStepUnavailable();
    error InsufficientCursorGas();

    modifier nonReentrant() {
        if (_entered != 1) revert ReentrantCall();
        _entered = 2;
        _;
        _entered = 1;
    }

    constructor() {
        if (block.chainid != ROBINHOOD_CHAIN_ID || V4_POOL_MANAGER.code.length == 0 || LIVE_Q.code.length == 0) {
            revert WrongChainOrDeployment();
        }
        poolManager = IUserPaidQPoolManager(V4_POOL_MANAGER);
        quoteToken = IUserPaidQCursor(LIVE_Q);
        poolId = keccak256(abi.encode(address(0), LIVE_Q, Q_ETH_FEE, Q_ETH_TICK_SPACING, address(0)));
    }

    function poolKey() public pure returns (HooklessQuoteBuyPoolKey memory) {
        return HooklessQuoteBuyPoolKey({
            currency0: address(0), currency1: LIVE_Q, fee: Q_ETH_FEE,
            tickSpacing: Q_ETH_TICK_SPACING, hooks: address(0)
        });
    }

    /// @notice Buy Q with exactly msg.value ETH, deliver it to recipient,
    /// then require Q's next cursor action to report success.
    function buyQAndProcess(uint256 minQOut, address recipient, uint64 deadline)
        external payable nonReentrant returns (uint256 qOut)
    {
        _validate(msg.value, minQOut, recipient, deadline);
        uint256 ethBefore = address(this).balance - msg.value;
        uint256 qBefore = quoteToken.balanceOf(address(this));
        bytes memory data = abi.encode(uint8(0), msg.value, minQOut);
        _callbackHash = keccak256(data);
        qOut = abi.decode(poolManager.unlock(data), (uint256));
        delete _callbackHash;
        if (address(this).balance != ethBefore || quoteToken.balanceOf(address(this)) - qBefore != qOut) {
            revert WrongSettlement();
        }
        if (!quoteToken.transfer(recipient, qOut) || quoteToken.balanceOf(address(this)) != qBefore) {
            revert TokenTransferFailed();
        }
        _processCursor(msg.sender);
        emit Bought(msg.sender, recipient, msg.value, qOut);
    }

    /// @notice Sell exactly qIn Q from msg.sender, send ETH to recipient,
    /// then require Q's next cursor action to report success. The seller
    /// must approve this router for qIn first.
    function sellQAndProcess(uint256 qIn, uint256 minEthOut, address recipient, uint64 deadline)
        external nonReentrant returns (uint256 ethOut)
    {
        _validate(qIn, minEthOut, recipient, deadline);
        uint256 ethBefore = address(this).balance;
        uint256 qBefore = quoteToken.balanceOf(address(this));
        if (!quoteToken.transferFrom(msg.sender, address(this), qIn) ||
            quoteToken.balanceOf(address(this)) - qBefore != qIn) revert TokenTransferFailed();
        bytes memory data = abi.encode(uint8(1), qIn, minEthOut);
        _callbackHash = keccak256(data);
        ethOut = abi.decode(poolManager.unlock(data), (uint256));
        delete _callbackHash;
        if (quoteToken.balanceOf(address(this)) != qBefore || address(this).balance - ethBefore != ethOut) {
            revert WrongSettlement();
        }
        _processCursor(msg.sender);
        (bool sent,) = recipient.call{value: ethOut}("");
        if (!sent || address(this).balance != ethBefore) revert NativeTransferFailed();
        emit Sold(msg.sender, recipient, qIn, ethOut);
    }

    function unlockCallback(bytes calldata data) external returns (bytes memory) {
        if (msg.sender != address(poolManager) || _entered != 2 || _callbackHash != keccak256(data)) {
            revert UnauthorizedCallback();
        }
        (uint8 side, uint256 amountIn, uint256 minOut) = abi.decode(data, (uint8, uint256, uint256));
        if (side == 0) return abi.encode(_buyInsideUnlock(amountIn, minOut));
        if (side == 1) return abi.encode(_sellInsideUnlock(amountIn, minOut));
        revert InvalidTrade();
    }

    function _buyInsideUnlock(uint256 ethIn, uint256 minQOut) private returns (uint256 qOut) {
        int256 packed = poolManager.swap(poolKey(), HooklessQuoteBuySwapParams({
            zeroForOne: true, amountSpecified: -int256(ethIn),
            sqrtPriceLimitX96: MIN_SQRT_PRICE_PLUS_ONE
        }), "");
        int128 nativeDelta = int128(packed >> 128);
        int128 quoteDelta = int128(packed);
        if (int256(nativeDelta) != -int256(ethIn)) revert PartialFill();
        if (quoteDelta <= 0 || uint256(uint128(quoteDelta)) < minQOut) revert InsufficientOutput();
        qOut = uint256(uint128(quoteDelta));
        if (poolManager.settle{value: ethIn}() != ethIn) revert WrongSettlement();
        poolManager.take(LIVE_Q, address(this), qOut);
    }

    function _sellInsideUnlock(uint256 qIn, uint256 minEthOut) private returns (uint256 ethOut) {
        int256 packed = poolManager.swap(poolKey(), HooklessQuoteBuySwapParams({
            zeroForOne: false, amountSpecified: -int256(qIn),
            sqrtPriceLimitX96: MAX_SQRT_PRICE_MINUS_ONE
        }), "");
        int128 nativeDelta = int128(packed >> 128);
        int128 quoteDelta = int128(packed);
        if (int256(quoteDelta) != -int256(qIn)) revert PartialFill();
        if (nativeDelta <= 0 || uint256(uint128(nativeDelta)) < minEthOut) revert InsufficientOutput();
        ethOut = uint256(uint128(nativeDelta));
        poolManager.sync(LIVE_Q);
        if (!quoteToken.transfer(address(poolManager), qIn)) revert TokenTransferFailed();
        if (poolManager.settle() != qIn) revert WrongSettlement();
        poolManager.take(address(0), address(this), ethOut);
    }

    function _processCursor(address payer) private {
        uint256 callGas = uint256(quoteToken.transferStepGasLimit()) + 1_000_000;
        if (gasleft() <= callGas + callGas / 63 + 100_000) revert InsufficientCursorGas();
        (bool attempted, bool succeeded) = quoteToken.processNext{gas: callGas}();
        if (!attempted || !succeeded) revert CursorStepUnavailable();
        emit CursorProcessed(payer, attempted, succeeded);
    }

    function _validate(uint256 amountIn, uint256 minOut, address recipient, uint64 deadline) private view {
        if (block.timestamp > deadline) revert Expired();
        if (amountIn == 0 || amountIn > uint256(uint128(type(int128).max)) || minOut == 0 ||
            recipient == address(0) || recipient == address(this) || recipient == address(poolManager)) {
            revert InvalidTrade();
        }
    }

    receive() external payable {
        if (msg.sender != address(poolManager) || _entered != 2) revert UnauthorizedCallback();
    }
}
