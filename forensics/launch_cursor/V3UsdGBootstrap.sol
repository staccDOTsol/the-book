// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {HooklessQuoteBuyAdapter} from "./HooklessQuoteBuyAdapter.sol";

interface IV3BootstrapToken {
    function balanceOf(address account) external view returns (uint256);
    function transfer(address recipient, uint256 amount) external returns (bool);
    function transferFrom(address sender, address recipient, uint256 amount) external returns (bool);
    function approve(address spender, uint256 amount) external returns (bool);
}

interface IV3BootstrapQ is IV3BootstrapToken {
    function owner() external view returns (address);
    function name() external view returns (string memory);
    function symbol() external view returns (string memory);
    function tokenURI() external view returns (string memory);
    function v3Factory() external view returns (address);
    function usdg() external view returns (address);
    function v3PoolFee() external view returns (uint24);
    function bindCanonicalV3Pool(address pool) external;
}

interface IV3BootstrapFactory {
    function getPool(address tokenA, address tokenB, uint24 fee) external view returns (address);
    function feeAmountTickSpacing(uint24 fee) external view returns (int24);
}

interface IV3BootstrapPool {
    function token0() external view returns (address);
    function token1() external view returns (address);
    function fee() external view returns (uint24);
    function liquidity() external view returns (uint128);
    function slot0() external view returns (
        uint160 sqrtPriceX96, int24 tick, uint16 observationIndex, uint16 observationCardinality,
        uint16 observationCardinalityNext, uint8 feeProtocol, bool unlocked
    );
}

interface IV3BootstrapPositionManager {
    struct MintParams {
        address token0;
        address token1;
        uint24 fee;
        int24 tickLower;
        int24 tickUpper;
        uint256 amount0Desired;
        uint256 amount1Desired;
        uint256 amount0Min;
        uint256 amount1Min;
        address recipient;
        uint256 deadline;
    }

    function createAndInitializePoolIfNecessary(address token0, address token1, uint24 fee, uint160 sqrtPriceX96)
        external returns (address pool);
    function mint(MintParams calldata params)
        external payable returns (uint256 tokenId, uint128 liquidity, uint256 amount0, uint256 amount1);
    function ownerOf(uint256 tokenId) external view returns (address);
    function factory() external view returns (address);
}

interface IV3BootstrapRouter {
    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        uint24 fee;
        address recipient;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 sqrtPriceLimitX96;
    }

    function exactInputSingle(ExactInputSingleParams calldata params) external payable returns (uint256 amountOut);
    function factory() external view returns (address);
}

interface IV3BootstrapStateView {
    function getSlot0(bytes32 poolId)
        external view returns (uint160 sqrtPriceX96, int24 tick, uint24 protocolFee, uint24 lpFee);
}

/// @notice One owner transaction, after a replacement Q has launched through
/// Pools.xyz Instant Launch on the canonical v4 Q/ETH pool. The owner supplies
/// USDG and either buys Q from that v4 pool with msg.value or supplies Q from
/// an existing balance. The first v3 NFT and all leftovers belong to owner.
/// @dev This does not create the Pools.xyz v4 launch or mint extra Q. A fresh
/// Q/USDG v3 pool is required; a precreated pool causes the whole call to revert.
contract V3UsdGBootstrap {
    uint256 private constant ROBINHOOD_CHAIN_ID = 4663;
    uint24 private constant V3_FEE = 3_000;
    uint24 private constant V4_FEE = 2_500;
    int24 private constant V4_TICK_SPACING = 25;

    address public constant V3_FACTORY = 0x1f7d7550B1b028f7571E69A784071F0205FD2EfA;
    address public constant V3_POSITION_MANAGER = 0x73991a25C818Bf1f1128dEAaB1492D45638DE0D3;
    address public constant V3_ROUTER = 0xCaf681a66D020601342297493863E78C959E5cb2;
    address public constant USDG = 0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168;
    address public constant LIVE_Q_METADATA = 0x0E34d0792032Ffc54C058751cF048dA347193472;
    address public constant V4_POOL_MANAGER = 0x8366a39CC670B4001A1121B8F6A443A643e40951;
    address public constant V4_STATE_VIEW = 0xF3334192D15450CdD385c8B70e03f9A6bD9E673b;

    struct Plan {
        uint256 qInventoryIn;
        uint256 minQFromV4;
        uint256 qForLp;
        uint256 usdgForLp;
        uint256 minQInLp;
        uint256 minUsdgInLp;
        uint256 usdgForBuy;
        uint256 minQFromV3Buy;
        uint160 initialSqrtPriceX96;
        uint160 v3BuySqrtPriceLimitX96;
        int24 tickLower;
        int24 tickUpper;
        uint64 deadline;
    }

    IV3BootstrapQ public immutable q;
    address public immutable owner;
    HooklessQuoteBuyAdapter public immutable v4Buyer;
    bytes32 public immutable v4PoolId;

    uint256 private _entered = 1;

    event V3Bootstrapped(
        address indexed pool, uint256 indexed tokenId, address indexed owner,
        uint256 ethSpentForQ, uint256 qAcquired, uint128 lpLiquidity,
        uint256 qInLp, uint256 usdgInLp, uint256 usdgBoughtWith, uint256 qBought
    );

    error WrongDeployment();
    error WrongQMetadataOrOwner();
    error NoInstantLaunchPool();
    error Unauthorized();
    error ReentrantCall();
    error InvalidPlan();
    error PoolAlreadyExists();
    error WrongV3Pool();
    error WrongMint();
    error WrongBuy();
    error TokenCallFailed();

    modifier onlyOwner() {
        if (msg.sender != owner) revert Unauthorized();
        _;
    }

    modifier nonReentrant() {
        if (_entered != 1) revert ReentrantCall();
        _entered = 2;
        _;
        _entered = 1;
    }

    constructor(address q_, address owner_) {
        if (
            block.chainid != ROBINHOOD_CHAIN_ID || q_ == address(0) || owner_ == address(0)
                || q_.code.length == 0 || USDG.code.length == 0 || V3_FACTORY.code.length == 0
                || V3_POSITION_MANAGER.code.length == 0 || V3_ROUTER.code.length == 0
                || V4_POOL_MANAGER.code.length == 0 || V4_STATE_VIEW.code.length == 0
                || IV3BootstrapPositionManager(V3_POSITION_MANAGER).factory() != V3_FACTORY
                || IV3BootstrapRouter(V3_ROUTER).factory() != V3_FACTORY
                || IV3BootstrapFactory(V3_FACTORY).feeAmountTickSpacing(V3_FEE) != 60
        ) revert WrongDeployment();

        IV3BootstrapQ token = IV3BootstrapQ(q_);
        IV3BootstrapQ live = IV3BootstrapQ(LIVE_Q_METADATA);
        if (
            token.owner() != owner_ || token.v3Factory() != V3_FACTORY || token.usdg() != USDG
                || token.v3PoolFee() != V3_FEE
                || keccak256(bytes(token.name())) != keccak256(bytes(live.name()))
                || keccak256(bytes(token.symbol())) != keccak256(bytes(live.symbol()))
                || keccak256(bytes(token.tokenURI())) != keccak256(bytes(live.tokenURI()))
        ) revert WrongQMetadataOrOwner();

        q = token;
        owner = owner_;
        v4PoolId = keccak256(abi.encode(address(0), q_, V4_FEE, V4_TICK_SPACING, address(0)));
        _requireInstantLaunchPool();
        v4Buyer = new HooklessQuoteBuyAdapter(V4_POOL_MANAGER, q_, V4_FEE, V4_TICK_SPACING, address(this));
    }

    /// @notice Uses the canonical deployed v3 position manager and router.
    /// Owner must approve this contract for qInventoryIn (inventory mode) and
    /// usdgForLp + usdgForBuy. The buy price limit and both LP minima are
    /// supplied by the owner and must be positive. One external call is atomic.
    function bootstrap(Plan calldata p)
        external payable onlyOwner nonReentrant
        returns (address pool, uint256 tokenId, uint256 qBought)
    {
        if (
            p.deadline < block.timestamp || p.qForLp == 0 || p.usdgForLp == 0
                || p.minQInLp == 0 || p.minUsdgInLp == 0
                || p.minQInLp > p.qForLp || p.minUsdgInLp > p.usdgForLp
                || p.usdgForBuy == 0 || p.minQFromV3Buy == 0
                || p.initialSqrtPriceX96 == 0 || p.v3BuySqrtPriceLimitX96 == 0
                || p.tickLower >= p.tickUpper
                || (msg.value == 0 && (p.qInventoryIn < p.qForLp || p.minQFromV4 != 0))
                || (msg.value != 0 && (p.qInventoryIn != 0 || p.minQFromV4 < p.qForLp))
        ) revert InvalidPlan();
        if (IV3BootstrapFactory(V3_FACTORY).getPool(address(q), USDG, V3_FEE) != address(0)) {
            revert PoolAlreadyExists();
        }
        _requireInstantLaunchPool();

        uint256 qBefore = q.balanceOf(address(this));
        uint256 usdgBefore = IV3BootstrapToken(USDG).balanceOf(address(this));
        uint256 qAcquired;
        if (msg.value != 0) {
            qAcquired = v4Buyer.buyQ{value: msg.value}(p.minQFromV4, address(this), p.deadline);
        } else {
            _transferFrom(address(q), owner, address(this), p.qInventoryIn);
            qAcquired = p.qInventoryIn;
        }
        if (q.balanceOf(address(this)) - qBefore != qAcquired) revert TokenCallFailed();

        uint256 usdgIn = p.usdgForLp + p.usdgForBuy;
        _transferFrom(USDG, owner, address(this), usdgIn);
        if (IV3BootstrapToken(USDG).balanceOf(address(this)) - usdgBefore != usdgIn) {
            revert TokenCallFailed();
        }

        bool qIs0 = address(q) < USDG;
        address token0 = qIs0 ? address(q) : USDG;
        address token1 = qIs0 ? USDG : address(q);
        pool = IV3BootstrapPositionManager(V3_POSITION_MANAGER).createAndInitializePoolIfNecessary(
            token0, token1, V3_FEE, p.initialSqrtPriceX96
        );
        if (
            pool == address(0) || pool != IV3BootstrapFactory(V3_FACTORY).getPool(address(q), USDG, V3_FEE)
                || IV3BootstrapPool(pool).token0() != token0 || IV3BootstrapPool(pool).token1() != token1
                || IV3BootstrapPool(pool).fee() != V3_FEE
        ) revert WrongV3Pool();
        (uint160 startPrice,,,,,,) = IV3BootstrapPool(pool).slot0();
        if (startPrice != p.initialSqrtPriceX96) revert WrongV3Pool();

        _approve(address(q), V3_POSITION_MANAGER, p.qForLp);
        _approve(USDG, V3_POSITION_MANAGER, p.usdgForLp);
        uint128 liquidity;
        uint256 amount0;
        uint256 amount1;
        (tokenId, liquidity, amount0, amount1) = IV3BootstrapPositionManager(V3_POSITION_MANAGER).mint(
            IV3BootstrapPositionManager.MintParams({
                token0: token0, token1: token1, fee: V3_FEE,
                tickLower: p.tickLower, tickUpper: p.tickUpper,
                amount0Desired: qIs0 ? p.qForLp : p.usdgForLp,
                amount1Desired: qIs0 ? p.usdgForLp : p.qForLp,
                amount0Min: qIs0 ? p.minQInLp : p.minUsdgInLp,
                amount1Min: qIs0 ? p.minUsdgInLp : p.minQInLp,
                recipient: owner, deadline: p.deadline
            })
        );
        _approve(address(q), V3_POSITION_MANAGER, 0);
        _approve(USDG, V3_POSITION_MANAGER, 0);
        uint256 qInLp = qIs0 ? amount0 : amount1;
        uint256 usdgInLp = qIs0 ? amount1 : amount0;
        if (
            liquidity == 0 || IV3BootstrapPool(pool).liquidity() == 0
                || IV3BootstrapPositionManager(V3_POSITION_MANAGER).ownerOf(tokenId) != owner
                || qInLp < p.minQInLp || qInLp > p.qForLp
                || usdgInLp < p.minUsdgInLp || usdgInLp > p.usdgForLp
        ) revert WrongMint();

        // Q starts charging v3 pool transfers only after its first LP mint.
        // The following router buy is the first transfer through the bound pool.
        q.bindCanonicalV3Pool(pool);
        _approve(USDG, V3_ROUTER, p.usdgForBuy);
        qBought = IV3BootstrapRouter(V3_ROUTER).exactInputSingle(
            IV3BootstrapRouter.ExactInputSingleParams({
                tokenIn: USDG, tokenOut: address(q), fee: V3_FEE, recipient: owner,
                amountIn: p.usdgForBuy, amountOutMinimum: p.minQFromV3Buy,
                sqrtPriceLimitX96: p.v3BuySqrtPriceLimitX96
            })
        );
        _approve(USDG, V3_ROUTER, 0);
        // Router02 can stop early at a sqrt-price limit. Requiring exact
        // USDG consumption makes the owner's stated buy size unambiguous.
        if (
            qBought < p.minQFromV3Buy
                || IV3BootstrapToken(USDG).balanceOf(address(this)) - usdgBefore
                    != p.usdgForLp - usdgInLp
        ) revert WrongBuy();

        uint256 qRefund = q.balanceOf(address(this)) - qBefore;
        uint256 usdgRefund = IV3BootstrapToken(USDG).balanceOf(address(this)) - usdgBefore;
        if (qRefund != 0) _transfer(address(q), owner, qRefund);
        if (usdgRefund != 0) _transfer(USDG, owner, usdgRefund);
        emit V3Bootstrapped(
            pool, tokenId, owner, msg.value, qAcquired, liquidity,
            qInLp, usdgInLp, p.usdgForBuy, qBought
        );
    }

    function _requireInstantLaunchPool() private view {
        (uint160 sqrtPriceX96,,, uint24 lpFee) = IV3BootstrapStateView(V4_STATE_VIEW).getSlot0(v4PoolId);
        if (sqrtPriceX96 == 0 || lpFee != V4_FEE) revert NoInstantLaunchPool();
    }

    function _transfer(address token, address recipient, uint256 amount) private {
        _callToken(token, abi.encodeWithSelector(IV3BootstrapToken.transfer.selector, recipient, amount));
    }

    function _transferFrom(address token, address sender, address recipient, uint256 amount) private {
        _callToken(token, abi.encodeWithSelector(IV3BootstrapToken.transferFrom.selector, sender, recipient, amount));
    }

    function _approve(address token, address spender, uint256 amount) private {
        _callToken(token, abi.encodeWithSelector(IV3BootstrapToken.approve.selector, spender, amount));
    }

    function _callToken(address token, bytes memory data) private {
        (bool ok, bytes memory result) = token.call(data);
        if (!ok || (result.length != 0 && (result.length != 32 || !abi.decode(result, (bool))))) {
            revert TokenCallFailed();
        }
    }
}
