// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

/// @notice Narrow interfaces keep this permissionless observer independent of
/// the executor implementation. The executor rechecks the live state when it
/// records entry, so a caller cannot fabricate a range crossing.
interface IInspectedExecutor {
    function controller() external view returns (address);
    function activePositionCount(address token) external view returns (uint8);
    function inspectTranche(address token, uint8 tranche) external view returns (
        uint160 sqrtPriceX96,
        bool inBand,
        bool atQuoteBoundary,
        bool atTokenBoundary,
        bool enteredBand
    );
    function markEnteredTranche(address token, uint8 tranche) external;
    function previewHarvest(address token) external view returns (uint256 grossEthValue, uint256 estimatedGasUnits);
}

interface IInspectedCursor {
    function executor() external view returns (address);
    function exitNotifier() external view returns (address);
    function notifyBandEntered(address token) external returns (bool);
    function notifyExitReady(address token, bool allQuote) external returns (bool);
    function notifyHarvestReady(address token) external returns (bool);
}

/// @notice A hookless, permissionless snapshot of one owned X/Q position.
/// Callers must still submit transactions: an ERC-20 transfer does not expose
/// every pool's tick or automatically inspect all active positions.
contract PositionInspector {
    IInspectedExecutor public immutable executor;
    IInspectedCursor public cursor;

    event CursorBound(address indexed cursor);
    event PositionObserved(address indexed token, uint160 sqrtPriceX96, bool entered, bool exitReady, bool allQuote);

    error InvalidAddress();
    error NotBound();

    constructor(address executor_) {
        if (executor_.code.length == 0) revert InvalidAddress();
        executor = IInspectedExecutor(executor_);
    }

    /// @notice The Q deployment embeds this inspector address. Binding is
    /// permissionless because both reciprocal links are checked on chain.
    function bindCursor(address cursor_) external {
        if (
            address(cursor) != address(0) || cursor_.code.length == 0 ||
            IInspectedCursor(cursor_).exitNotifier() != address(this) ||
            IInspectedCursor(cursor_).executor() != address(executor) ||
            executor.controller() != cursor_
        ) revert InvalidAddress();
        cursor = IInspectedCursor(cursor_);
        emit CursorBound(cursor_);
    }

    /// @notice Inspect the *current* v4 price. A complete jump from the
    /// opening Q-only side to all-X proves that the position crossed its band
    /// even when no intermediate block was inspected. A prior entry followed
    /// by either one-sided boundary produces an exit report.
    function poke(address token) external returns (bool entered, bool exitReady) {
        if (address(cursor) == address(0)) revert NotBound();
        uint160 observedPrice;
        bool observedQuoteBoundary;
        for (uint8 i; i < 3; ++i) {
            try executor.inspectTranche(token, i) returns (
                uint160 sqrtPriceX96, bool inBand, bool atQuoteBoundary,
                bool atTokenBoundary, bool wasEntered
            ) {
                observedPrice = sqrtPriceX96;
                bool trancheEntered = wasEntered;
                if (!trancheEntered && (inBand || atTokenBoundary)) {
                    executor.markEnteredTranche(token, i);
                    cursor.notifyBandEntered(token);
                    trancheEntered = true;
                } else if (trancheEntered) {
                    cursor.notifyBandEntered(token);
                }
                if (trancheEntered) entered = true;
                if (!exitReady && trancheEntered && (atQuoteBoundary || atTokenBoundary)) {
                    exitReady = cursor.notifyExitReady(token, atQuoteBoundary);
                    if (exitReady) observedQuoteBoundary = atQuoteBoundary;
                }
            } catch {}
        }
        emit PositionObserved(token, observedPrice, entered, exitReady, observedQuoteBoundary);
    }

    /// @notice Queue a signer-quoted, gas-positive interim LP-fee claim. A
    /// position may earn fees and then remain active outside its range, so
    /// this deliberately does not require a current in-band observation.
    function pokeHarvest(address token) external returns (bool queued) {
        if (address(cursor) == address(0)) revert NotBound();
        if (executor.activePositionCount(token) == 0) return false;
        try executor.previewHarvest(token) returns (uint256 grossEthValue, uint256 estimatedGasUnits) {
            if (grossEthValue != 0 && estimatedGasUnits != 0) {
                return cursor.notifyHarvestReady(token);
            }
        } catch {}
        return false;
    }
}
