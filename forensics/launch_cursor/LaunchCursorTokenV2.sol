// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {LaunchCursorToken} from "./LaunchCursorToken.sol";

interface IV3QPoolFactory {
    function getPool(address tokenA, address tokenB, uint24 fee) external view returns (address);
}

interface IV3QPoolIdentity {
    function liquidity() external view returns (uint128);
}

contract V3CanonicalPoolVerifier {
    IV3QPoolFactory private immutable _factory;
    address private immutable _usdg;
    uint24 private immutable _fee;

    constructor(address factory_, address usdg_, uint24 fee_) {
        _factory = IV3QPoolFactory(factory_);
        _usdg = usdg_;
        _fee = fee_;
    }

    function isReady(address q, address pool) external view returns (bool) {
        return pool != address(0) && _factory.getPool(q, _usdg, _fee) == pool &&
            IV3QPoolIdentity(pool).liquidity() != 0;
    }
}

/// @notice A fresh Q with the complete launch cursor and an additional
/// transfer trigger for its canonical Uniswap v3 Q/USDG pool. The v4 launch
/// market and the X/Q queue remain the inherited LaunchCursorToken system.
/// @dev The first LP mint happens before binding so setup transfers do not
/// use the new pool trigger. Anyone may bind the one verified, liquid pool;
/// its factory, quote asset and fee are fixed by the Q deployer. This permits
/// an atomic v3 pool setup, bind and first swap by a separate orchestrator.
contract LaunchCursorTokenV2 is LaunchCursorToken {
    struct V3PoolConfig {
        address factory;
        address usdg;
        uint24 fee;
    }

    IV3QPoolFactory public immutable v3Factory;
    address public immutable usdg;
    uint24 public immutable v3PoolFee;
    address public canonicalV3Pool;
    V3CanonicalPoolVerifier private immutable _poolVerifier;

    event CanonicalV3PoolBound(address indexed pool);

    error V3QueueStepFailed();

    constructor(
        Metadata memory metadata_,
        uint256 initialSupply_,
        address factory_,
        address executor_,
        address exitNotifier_,
        uint64 retryDelaySeconds_,
        uint32 transferStepGasLimit_,
        uint256 harvestGasPriceCeilingWei_,
        address[] memory settlementEndpoints_,
        V3PoolConfig memory v3Pool_
    ) LaunchCursorToken(
        metadata_, initialSupply_, factory_, executor_, exitNotifier_,
        retryDelaySeconds_, transferStepGasLimit_, harvestGasPriceCeilingWei_,
        settlementEndpoints_
    ) {
        if (
            v3Pool_.factory.code.length == 0 || v3Pool_.usdg.code.length == 0 ||
            v3Pool_.usdg == address(this) || v3Pool_.fee == 0
        ) revert BadConfiguration();
        v3Factory = IV3QPoolFactory(v3Pool_.factory);
        usdg = v3Pool_.usdg;
        v3PoolFee = v3Pool_.fee;
        _poolVerifier = new V3CanonicalPoolVerifier(v3Pool_.factory, v3Pool_.usdg, v3Pool_.fee);
    }

    /// @notice Bind only the already initialized, liquid pool registered by
    /// the factory for this Q, USDG and the deployer's chosen fee tier.
    /// The binding is irreversible so trades cannot redirect the trigger.
    function bindCanonicalV3Pool(address pool) external {
        if (canonicalV3Pool != address(0) || !_poolVerifier.isReady(address(this), pool)) {
            revert BadConfiguration();
        }
        canonicalV3Pool = pool;
        emit CanonicalV3PoolBound(pool);
    }

    /// @dev A v3 swap cannot settle when a ready action fails, runs out of
    /// bounded gas or only performs scheduler housekeeping. This makes the
    /// swap caller pay for one successful dequeue. A stale action can freeze
    /// this pool until repaired or the owner calls inherited setAutomatic(false).
    function _update(address from, address to, uint256 amount) internal override {
        bool poolTransfer = _isCanonicalPoolTransfer(from, to, amount);
        super._update(from, to, amount);
        if (
            !poolTransfer || !automaticEnabled || _isProcessing() || !_hasReadyAction()
        ) return;

        uint256 stepGas = _transferStepCallGas();
        uint256 completedBefore = successfulExecutorSteps;
        try this.processTransferStep{gas: stepGas}() returns (bool, bool) {
            // processTransferStep may return (true, true) after only consuming
            // a pending report or stale entry, so check executor progress.
            if (successfulExecutorSteps != completedBefore + 1) revert V3QueueStepFailed();
        } catch {
            revert V3QueueStepFailed();
        }
    }

    function _shouldAttemptTransferStep(address from, address to) internal view override returns (bool) {
        // Suppress the inherited direct-EOA trigger for direct sells to the
        // bound pool; the strict v3 path above performs the only queue call.
        return !_isCanonicalPoolTransfer(from, to, 1) && super._shouldAttemptTransferStep(from, to);
    }

    function _isCanonicalPoolTransfer(address from, address to, uint256 amount) private view returns (bool) {
        address pool = canonicalV3Pool;
        return pool != address(0) && amount != 0 && from != address(0) &&
            to != address(0) && (from == pool || to == pool);
    }
}
