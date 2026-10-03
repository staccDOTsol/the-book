// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {
    HooklessQuoteBuyAdapter,
    HooklessQuoteBuyPoolKey,
    IHooklessQuoteBuyPoolManager
} from "./HooklessQuoteBuyAdapter.sol";

interface IHooklessQuoteBuyForkVm {
    function createSelectFork(string calldata rpcUrl) external returns (uint256);
    function deal(address account, uint256 newBalance) external;
    function startPrank(address sender) external;
    function stopPrank() external;
    function expectRevert(bytes4 selector) external;
}

interface IHooklessQuoteBuyForkPoolManager is IHooklessQuoteBuyPoolManager {
    struct ModifyLiquidityParams {
        int24 tickLower;
        int24 tickUpper;
        int256 liquidityDelta;
        bytes32 salt;
    }

    function initialize(HooklessQuoteBuyPoolKey memory key, uint160 sqrtPriceX96) external returns (int24 tick);
    function modifyLiquidity(
        HooklessQuoteBuyPoolKey memory key,
        ModifyLiquidityParams memory params,
        bytes calldata hookData
    ) external returns (int256 callerDelta, int256 feesAccrued);
    function sync(address currency) external;
}

contract HooklessQuoteBuyForkQ {
    string public constant name = "Fork Synthetic Q";
    string public constant symbol = "Q";
    uint8 public constant decimals = 18;

    mapping(address => uint256) public balanceOf;

    function mint(address to, uint256 amount) external {
        balanceOf[to] += amount;
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        balanceOf[msg.sender] -= amount;
        balanceOf[to] += amount;
        return true;
    }
}

/// @dev Creates and funds the synthetic pool against Robinhood's actual v4
/// PoolManager. The test's liquidity and Q exist only in its local fork.
contract HooklessQuoteBuyForkSeeder {
    IHooklessQuoteBuyForkPoolManager public immutable poolManager;
    HooklessQuoteBuyForkQ public immutable quoteToken;
    uint24 public immutable fee;
    int24 public immutable tickSpacing;

    constructor(address manager_, address quote_, uint24 fee_, int24 tickSpacing_) {
        poolManager = IHooklessQuoteBuyForkPoolManager(manager_);
        quoteToken = HooklessQuoteBuyForkQ(quote_);
        fee = fee_;
        tickSpacing = tickSpacing_;
    }

    function poolKey() public view returns (HooklessQuoteBuyPoolKey memory) {
        return HooklessQuoteBuyPoolKey({
            currency0: address(0), currency1: address(quoteToken), fee: fee, tickSpacing: tickSpacing, hooks: address(0)
        });
    }

    function seed() external {
        poolManager.initialize(poolKey(), uint160(1 << 96));
        poolManager.unlock("");
    }

    function unlockCallback(bytes calldata) external returns (bytes memory) {
        require(msg.sender == address(poolManager), "unexpected unlock caller");
        (int256 packedDelta,) = poolManager.modifyLiquidity(
            poolKey(),
            IHooklessQuoteBuyForkPoolManager.ModifyLiquidityParams({
                tickLower: -1_000, tickUpper: 1_000, liquidityDelta: int256(1_000 ether), salt: bytes32(0)
            }),
            ""
        );
        int128 ethDelta = int128(packedDelta >> 128);
        int128 quoteDelta = int128(packedDelta);
        require(ethDelta < 0 && quoteDelta < 0, "liquidity not two sided");
        uint256 ethOwed = uint256(-int256(ethDelta));
        uint256 quoteOwed = uint256(-int256(quoteDelta));
        require(poolManager.settle{value: ethOwed}() == ethOwed, "ETH liquidity settlement mismatch");
        poolManager.sync(address(quoteToken));
        require(quoteToken.transfer(address(poolManager), quoteOwed), "Q liquidity transfer failed");
        require(poolManager.settle() == quoteOwed, "Q liquidity settlement mismatch");
        return "";
    }

    receive() external payable {}
}

/// @notice Fork-only adapter test. No call is broadcast to Robinhood.
/// Q and its Q/ETH pool are synthesized locally because Q has not launched.
contract HooklessQuoteBuyForkTest {
    IHooklessQuoteBuyForkVm private constant vm =
        IHooklessQuoteBuyForkVm(address(uint160(uint256(keccak256("hevm cheat code")))));

    address private constant POOL_MANAGER = 0x8366a39CC670B4001A1121B8F6A443A643e40951;
    uint24 private constant FEE = 2_500;
    int24 private constant TICK_SPACING = 25;

    function testBuyExactNativeEthInSyntheticQPool() external {
        vm.createSelectFork("https://rpc.mainnet.chain.robinhood.com");
        require(block.chainid == 4663, "not Robinhood");

        HooklessQuoteBuyForkQ q = new HooklessQuoteBuyForkQ();
        HooklessQuoteBuyForkSeeder seeder = new HooklessQuoteBuyForkSeeder(POOL_MANAGER, address(q), FEE, TICK_SPACING);
        q.mint(address(seeder), 1_000 ether);
        vm.deal(address(seeder), 100 ether);
        seeder.seed();

        HooklessQuoteBuyAdapter adapter =
            new HooklessQuoteBuyAdapter(POOL_MANAGER, address(q), FEE, TICK_SPACING, address(this));
        bytes32 expectedPoolId = keccak256(abi.encode(address(0), address(q), FEE, TICK_SPACING, address(0)));
        require(adapter.poolId() == expectedPoolId, "wrong Q/ETH pool ID");
        require(keccak256(abi.encode(adapter.poolKey())) == expectedPoolId, "wrong Q/ETH pool key");
        vm.deal(address(this), 1 ether);

        address recipient = address(0xCAFE);
        uint256 ethIn = 0.1 ether;
        uint64 deadline = uint64(block.timestamp + 60);
        uint256 sourceNativeBefore = address(this).balance;
        uint256 managerNativeBefore = POOL_MANAGER.balance;
        uint256 managerQuoteBefore = q.balanceOf(POOL_MANAGER);

        vm.deal(address(0xBEEF), 1 ether);
        vm.startPrank(address(0xBEEF));
        vm.expectRevert(HooklessQuoteBuyAdapter.Unauthorized.selector);
        adapter.buyQ{value: ethIn}(1, recipient, deadline);
        vm.stopPrank();

        vm.expectRevert(HooklessQuoteBuyAdapter.Expired.selector);
        adapter.buyQ{value: ethIn}(1, recipient, uint64(block.timestamp - 1));
        vm.expectRevert(HooklessQuoteBuyAdapter.InvalidAmount.selector);
        adapter.buyQ{value: ethIn}(0, recipient, deadline);
        vm.expectRevert(HooklessQuoteBuyAdapter.InsufficientQOutput.selector);
        adapter.buyQ{value: ethIn}(type(uint256).max, recipient, deadline);
        require(address(this).balance == sourceNativeBefore, "failed buy spent ETH");
        require(POOL_MANAGER.balance == managerNativeBefore, "failed buy settled ETH");
        require(q.balanceOf(POOL_MANAGER) == managerQuoteBefore, "failed buy moved Q");

        uint256 qOut = adapter.buyQ{value: ethIn}(1, recipient, deadline);
        require(qOut > 0, "no Q bought");
        require(address(this).balance == sourceNativeBefore - ethIn, "source ETH mismatch");
        require(POOL_MANAGER.balance == managerNativeBefore + ethIn, "manager ETH mismatch");
        require(q.balanceOf(POOL_MANAGER) == managerQuoteBefore - qOut, "manager Q mismatch");
        require(q.balanceOf(recipient) == qOut, "recipient Q mismatch");
        require(q.balanceOf(address(adapter)) == 0, "adapter retained Q");
        require(address(adapter).balance == 0, "adapter retained ETH");
    }

    receive() external payable {}
}
