// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {UserPaidQTradeRouter} from "./UserPaidQTradeRouter.sol";
import {HooklessQuoteBuyPoolKey, HooklessQuoteBuySwapParams} from "./HooklessQuoteBuyAdapter.sol";

interface VmUserPaidRoute {
    function chainId(uint256 chainId_) external;
    function etch(address target, bytes calldata code) external;
    function deal(address target, uint256 value) external;
}

interface IUserPaidUnlockCallback {
    function unlockCallback(bytes calldata data) external returns (bytes memory);
}

contract MockUserPaidQ {
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;
    uint256 public steps;
    bool public stepSuccess = true;
    bool public stepAvailable = true;
    bool public sawUnlocked;

    function mint(address to, uint256 amount) external { balanceOf[to] += amount; }
    function setStepSuccess(bool ok) external { stepSuccess = ok; }
    function setStepAvailable(bool available) external { stepAvailable = available; }
    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        return true;
    }
    function transfer(address to, uint256 amount) external returns (bool) {
        balanceOf[msg.sender] -= amount;
        balanceOf[to] += amount;
        return true;
    }
    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        uint256 permitted = allowance[from][msg.sender];
        if (permitted != type(uint256).max) allowance[from][msg.sender] = permitted - amount;
        balanceOf[from] -= amount;
        balanceOf[to] += amount;
        return true;
    }
    function transferStepGasLimit() external pure returns (uint32) { return 100_000; }
    function processNext() external returns (bool attempted, bool succeeded) {
        sawUnlocked = MockUserPaidPoolManager(payable(0x8366a39CC670B4001A1121B8F6A443A643e40951)).unlocked();
        require(!sawUnlocked, "cursor called before v4 lock closed");
        if (!stepAvailable) return (false, false);
        ++steps;
        return (true, stepSuccess);
    }
}

contract MockUserPaidPoolManager {
    address private constant LIVE_Q = 0x0E34d0792032Ffc54C058751cF048dA347193472;
    bool public unlocked;
    uint256 private _syncedQuoteBalance;

    function unlock(bytes calldata data) external returns (bytes memory result) {
        require(!unlocked, "AlreadyUnlocked");
        unlocked = true;
        result = IUserPaidUnlockCallback(msg.sender).unlockCallback(data);
        unlocked = false;
    }

    function swap(HooklessQuoteBuyPoolKey memory key, HooklessQuoteBuySwapParams memory params, bytes calldata)
        external view returns (int256 packed)
    {
        require(unlocked, "locked");
        require(key.currency0 == address(0) && key.currency1 == LIVE_Q &&
            key.fee == 2_500 && key.tickSpacing == 25 && key.hooks == address(0), "wrong pool");
        require(params.amountSpecified < 0, "not exact input");
        int256 input = -params.amountSpecified;
        int128 nativeDelta;
        int128 quoteDelta;
        if (params.zeroForOne) {
            nativeDelta = -int128(input);
            quoteDelta = int128(input * 100);
        } else {
            nativeDelta = int128(input / 100);
            quoteDelta = -int128(input);
        }
        packed = (int256(nativeDelta) << 128) | int256(uint256(uint128(quoteDelta)));
    }

    function sync(address currency) external {
        require(unlocked && currency == LIVE_Q, "wrong sync");
        _syncedQuoteBalance = MockUserPaidQ(LIVE_Q).balanceOf(address(this));
    }

    function settle() external payable returns (uint256 paid) {
        require(unlocked, "locked");
        if (msg.value != 0) return msg.value;
        paid = MockUserPaidQ(LIVE_Q).balanceOf(address(this)) - _syncedQuoteBalance;
        _syncedQuoteBalance = 0;
    }

    function take(address currency, address to, uint256 amount) external {
        require(unlocked, "locked");
        if (currency == LIVE_Q) {
            require(MockUserPaidQ(LIVE_Q).transfer(to, amount), "Q take failed");
        } else {
            require(currency == address(0), "wrong currency");
            (bool ok,) = to.call{value: amount}("");
            require(ok, "ETH take failed");
        }
    }

    receive() external payable {}
}

contract UserPaidQTradeRouterTest {
    VmUserPaidRoute private constant vm =
        VmUserPaidRoute(address(uint160(uint256(keccak256("hevm cheat code")))));
    address private constant LIVE_Q = 0x0E34d0792032Ffc54C058751cF048dA347193472;
    address private constant MANAGER = 0x8366a39CC670B4001A1121B8F6A443A643e40951;
    address private constant RECIPIENT = address(0xBEEF);

    UserPaidQTradeRouter private router;
    MockUserPaidQ private q;

    function setUp() external {
        vm.chainId(4663);
        MockUserPaidQ qImpl = new MockUserPaidQ();
        MockUserPaidPoolManager managerImpl = new MockUserPaidPoolManager();
        vm.etch(LIVE_Q, address(qImpl).code);
        vm.etch(MANAGER, address(managerImpl).code);
        q = MockUserPaidQ(LIVE_Q);
        q.setStepSuccess(true);
        q.setStepAvailable(true);
        router = new UserPaidQTradeRouter();
        q.mint(MANAGER, 10_000 ether);
        vm.deal(MANAGER, 100 ether);
        vm.deal(address(this), 10 ether);
    }

    function testBuySettlesThenRunsOneStepInSameTransaction() external {
        uint256 out = router.buyQAndProcess{value: 1 ether}(99 ether, RECIPIENT, uint64(block.timestamp + 60));
        require(out == 100 ether && q.balanceOf(RECIPIENT) == 100 ether, "wrong Q receipt");
        require(q.steps() == 1 && !q.sawUnlocked(), "cursor did not run after unlock");
        require(address(router).balance == 0 && q.balanceOf(address(router)) == 0, "router retained funds");
    }

    function testSellSettlesThenRunsOneStepInSameTransaction() external {
        q.mint(address(this), 100 ether);
        require(q.approve(address(router), 100 ether), "approval failed");
        uint256 beforeEth = RECIPIENT.balance;
        uint256 out = router.sellQAndProcess(100 ether, 0.99 ether, RECIPIENT, uint64(block.timestamp + 60));
        require(out == 1 ether && RECIPIENT.balance == beforeEth + 1 ether, "wrong ETH receipt");
        require(q.steps() == 1 && !q.sawUnlocked(), "cursor did not run after unlock");
        require(address(router).balance == 0 && q.balanceOf(address(router)) == 0, "router retained funds");
    }

    function testRevertWhenCursorStepFailsRollsBackBuy() external {
        q.setStepSuccess(false);
        uint256 managerEth = MANAGER.balance;
        uint256 managerQ = q.balanceOf(MANAGER);
        (bool ok,) = address(router).call{value: 1 ether}(
            abi.encodeCall(router.buyQAndProcess, (1, RECIPIENT, uint64(block.timestamp + 60)))
        );
        require(!ok && q.steps() == 0, "failed cursor step did not roll back");
        require(MANAGER.balance == managerEth && q.balanceOf(MANAGER) == managerQ &&
            q.balanceOf(RECIPIENT) == 0, "failed route changed balances");
    }

    function testRevertWhenNoActionReadyRollsBackBuy() external {
        q.setStepAvailable(false);
        uint256 managerEth = MANAGER.balance;
        uint256 managerQ = q.balanceOf(MANAGER);
        (bool ok,) = address(router).call{value: 1 ether}(
            abi.encodeCall(router.buyQAndProcess, (1, RECIPIENT, uint64(block.timestamp + 60)))
        );
        require(!ok && q.steps() == 0, "empty queue did not revert");
        require(MANAGER.balance == managerEth && q.balanceOf(MANAGER) == managerQ &&
            q.balanceOf(RECIPIENT) == 0, "empty-queue route changed balances");
    }

    function testRevertWhenBuyOutputBelowMinimum() external {
        uint256 managerEth = MANAGER.balance;
        uint256 managerQ = q.balanceOf(MANAGER);
        (bool ok,) = address(router).call{value: 1 ether}(
            abi.encodeCall(router.buyQAndProcess, (101 ether, RECIPIENT, uint64(block.timestamp + 60)))
        );
        require(!ok && q.steps() == 0, "slippage check did not revert");
        require(MANAGER.balance == managerEth && q.balanceOf(MANAGER) == managerQ &&
            q.balanceOf(RECIPIENT) == 0, "failed slippage changed balances");
    }

    function testRevertWhenGasCannotFundCursorStep() external {
        uint256 managerEth = MANAGER.balance;
        uint256 managerQ = q.balanceOf(MANAGER);
        (bool ok,) = address(router).call{value: 1 ether, gas: 1_000_000}(
            abi.encodeCall(router.buyQAndProcess, (1, RECIPIENT, uint64(block.timestamp + 60)))
        );
        require(!ok && q.steps() == 0, "underfunded cursor route did not revert");
        require(MANAGER.balance == managerEth && q.balanceOf(MANAGER) == managerQ &&
            q.balanceOf(RECIPIENT) == 0, "underfunded route changed balances");
    }

    function testRevertWhenUnlockCallbackCalledDirectly() external {
        (bool ok,) = address(router).call(
            abi.encodeCall(router.unlockCallback, (abi.encode(uint8(0), 1 ether, 1 ether)))
        );
        require(!ok, "unauthorized callback succeeded");
    }

    receive() external payable {}
}
