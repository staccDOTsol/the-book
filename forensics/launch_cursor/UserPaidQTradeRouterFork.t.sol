// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {UserPaidQTradeRouter} from "./UserPaidQTradeRouter.sol";

interface VmUserPaidFork {
    function deal(address account, uint256 newBalance) external;
    function mockCall(address callee, bytes calldata data, bytes calldata returnData) external;
    function skip(bool skipTest) external;
}

interface ILiveUserPaidQ {
    function approve(address spender, uint256 amount) external returns (bool);
    function balanceOf(address account) external view returns (uint256);
}

/// @notice Exercises both swap directions against the actual Robinhood v4
/// Q/ETH pool on a local current-head fork. The Q.processNext return is mocked
/// because the live queued open may have an expired keeper price plan. The
/// separate offline tests prove that the route calls it after unlock.
contract UserPaidQTradeRouterForkTest {
    VmUserPaidFork private constant vm =
        VmUserPaidFork(address(uint160(uint256(keccak256("hevm cheat code")))));
    address private constant LIVE_Q = 0x0E34d0792032Ffc54C058751cF048dA347193472;
    bytes4 private constant PROCESS_NEXT = 0x4ba3eeaf;

    function testBuyThenSellThroughActualQEthPool() external {
        vm.skip(block.chainid != 4663);
        vm.deal(address(this), 1 ether);
        vm.mockCall(LIVE_Q, abi.encodePacked(PROCESS_NEXT), abi.encode(true, true));

        UserPaidQTradeRouter route = new UserPaidQTradeRouter();
        ILiveUserPaidQ q = ILiveUserPaidQ(LIVE_Q);
        uint64 deadline = uint64(block.timestamp + 60);
        uint256 qBefore = q.balanceOf(address(this));
        uint256 qOut = route.buyQAndProcess{value: 0.001 ether}(1, address(this), deadline);
        require(qOut > 1 && q.balanceOf(address(this)) == qBefore + qOut, "real Q buy mismatch");

        uint256 sellIn = qOut / 2;
        require(q.approve(address(route), sellIn), "Q approval failed");
        uint256 ethBefore = address(this).balance;
        uint256 ethOut = route.sellQAndProcess(sellIn, 1, address(this), deadline);
        require(ethOut > 0 && address(this).balance == ethBefore + ethOut, "real Q sell mismatch");
        require(q.balanceOf(address(this)) == qBefore + qOut - sellIn, "real Q balance mismatch");
        require(q.balanceOf(address(route)) == 0 && address(route).balance == 0, "route retained assets");
    }

    receive() external payable {}
}
