// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {PonsGraduatedSwapAdapter, IPonsGraduatedToken} from "./PonsGraduatedSwapAdapter.sol";

interface IPonsGraduatedForkVm {
    function createSelectFork(string calldata rpcUrl, uint256 blockNumber) external returns (uint256);
    function startPrank(address sender) external;
    function stopPrank() external;
    function expectRevert(bytes4 selector) external;
}

/// @notice Read-only Robinhood fork exercise against a factory-recorded,
/// native-ETH graduated Pons X. It sends no transaction to the live chain.
contract PonsGraduatedSwapForkTest {
    IPonsGraduatedForkVm private constant vm =
        IPonsGraduatedForkVm(address(uint160(uint256(keccak256("hevm cheat code")))));

    address private constant FACTORY = 0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e;
    address private constant X = 0x6C7C3113bFa9EeF3E716A4912D9B0dc47AEFE796;
    address private constant SOURCE = 0x2E05B44DC8682Aa5497eeB3f1C44dDa3f204F0CC;
    uint256 private constant FORK_BLOCK = 78_909_215;

    function testSellNativeGraduatedPoolOnRobinhoodFork() external {
        vm.createSelectFork("https://rpc.mainnet.chain.robinhood.com", FORK_BLOCK);
        require(block.chainid == 4663, "not Robinhood");

        PonsGraduatedSwapAdapter adapter = new PonsGraduatedSwapAdapter(FACTORY, SOURCE);
        (, bytes32 poolId) = adapter.registeredNativePool(X);
        require(poolId == 0x5c9b2c7f82c9f8693d1b7f6972cb9deaf823949a1106694df08e1839d62ed5e6, "wrong Pons pool");
        IPonsGraduatedToken token = IPonsGraduatedToken(X);
        uint256 amountIn = 1_000 ether;
        uint256 sourceBefore = token.balanceOf(SOURCE);
        uint256 recipientBefore = address(this).balance;
        require(sourceBefore >= amountIn, "source has insufficient X");

        vm.expectRevert(PonsGraduatedSwapAdapter.Unauthorized.selector);
        adapter.sellNativeGraduatedPool(X, amountIn, 1, payable(address(this)), uint64(block.timestamp + 60));

        vm.startPrank(SOURCE);
        require(token.approve(address(adapter), amountIn), "X approval failed");
        vm.expectRevert(PonsGraduatedSwapAdapter.InsufficientEthOutput.selector);
        adapter.sellNativeGraduatedPool(
            X, amountIn, type(uint256).max, payable(address(this)), uint64(block.timestamp + 60)
        );
        require(token.balanceOf(SOURCE) == sourceBefore, "failed swap moved source X");
        require(token.balanceOf(address(adapter)) == 0, "failed swap retained X");
        uint256 ethOut =
            adapter.sellNativeGraduatedPool(X, amountIn, 1, payable(address(this)), uint64(block.timestamp + 60));
        vm.stopPrank();

        require(ethOut > 0, "no ETH received");
        require(token.balanceOf(SOURCE) == sourceBefore - amountIn, "source X mismatch");
        require(token.balanceOf(address(adapter)) == 0, "adapter retained X");
        require(address(this).balance == recipientBefore + ethOut, "recipient ETH mismatch");
        require(address(adapter).balance == 0, "adapter retained ETH");
    }

    receive() external payable {}
}
