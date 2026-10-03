// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {
    PonsActiveExitAdapter,
    IPonsActiveExitFactory,
    IPonsActiveExitCurve,
    IPonsActiveExitToken
} from "./PonsActiveExitAdapter.sol";

interface IPonsActiveForkVm {
    function createSelectFork(string calldata rpcUrl) external returns (uint256);
    function deal(address account, uint256 newBalance) external;
    function expectRevert(bytes calldata revertData) external;
}

interface IPonsActiveForkCurve is IPonsActiveExitCurve {
    function buy(uint256 quoteIn, uint256 minTokensOut, address recipient)
        external
        payable
        returns (uint256 tokensOut);
}

/// @notice A real phase-0 native-ETH Pons launch at the pinned Robinhood block.
/// The test buys X and sells it through the adapter only inside the fork EVM.
contract PonsActiveExitForkTest {
    IPonsActiveForkVm private constant vm =
        IPonsActiveForkVm(address(uint160(uint256(keccak256("hevm cheat code")))));

    address private constant FACTORY = 0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e;
    address private constant X = 0xeB765696eE5905ce1D06D72280dEFB2cE426115d;
    address private constant CURVE = 0x9d4bcCd80332ba9CcCC75657B560Bb89265462aD;
    address payable private constant RECIPIENT = payable(address(0xBEEF));
    uint256 private constant BUY_ETH = 0.001 ether;
    bytes4 private constant CURVE_SLIPPAGE = bytes4(keccak256("SlippageExceeded(uint256,uint256)"));

    function testSellRealActiveNativeLaunchOnRobinhoodFork() external {
        vm.createSelectFork("https://rpc.mainnet.chain.robinhood.com");
        require(block.chainid == 4663, "not Robinhood");

        IPonsActiveExitFactory.LaunchedToken memory launch =
            IPonsActiveExitFactory(FACTORY).getLaunchedToken(X);
        require(launch.exists && launch.token == X && launch.curve == CURVE, "wrong factory launch");
        require(launch.phase == 0 && launch.pairToken == address(0), "not active native launch");

        IPonsActiveForkCurve curve = IPonsActiveForkCurve(CURVE);
        require(curve.factory() == FACTORY && curve.token() == X, "wrong curve binding");
        require(curve.isNativeQuote() && !curve.graduated() && !curve.readyToGraduate(), "curve not sellable");

        vm.deal(address(this), BUY_ETH);
        IPonsActiveExitToken token = IPonsActiveExitToken(X);
        uint256 sourceBeforeBuy = token.balanceOf(address(this));
        uint256 bought = curve.buy{value: BUY_ETH}(BUY_ETH, 1, address(this));
        require(bought > 0 && token.balanceOf(address(this)) == sourceBeforeBuy + bought, "buy X mismatch");

        PonsActiveExitAdapter adapter = new PonsActiveExitAdapter(FACTORY, address(this));
        (address registeredCurve, uint256 quotedEth) = adapter.previewNativeActiveSale(X, bought);
        require(registeredCurve == CURVE && quotedEth > 0, "bad active quote");
        require(token.approve(address(adapter), bought), "adapter approval failed");

        uint256 sourceXBefore = token.balanceOf(address(this));
        uint256 curveXBefore = token.balanceOf(CURVE);
        uint256 adapterXBefore = token.balanceOf(address(adapter));
        uint256 curveEthBefore = CURVE.balance;
        uint256 adapterEthBefore = address(adapter).balance;
        uint256 recipientEthBefore = RECIPIENT.balance;
        uint64 deadline = uint64(block.timestamp + 60);

        vm.expectRevert(abi.encodeWithSelector(CURVE_SLIPPAGE, quotedEth, quotedEth + 1));
        adapter.sellNativeActiveCurve(X, bought, quotedEth + 1, RECIPIENT, deadline);
        require(token.balanceOf(address(this)) == sourceXBefore, "failed sale debited X");
        require(token.balanceOf(CURVE) == curveXBefore, "failed sale credited curve X");
        require(token.balanceOf(address(adapter)) == adapterXBefore, "failed sale retained X");
        require(CURVE.balance == curveEthBefore, "failed sale debited curve ETH");
        require(address(adapter).balance == adapterEthBefore, "failed sale retained ETH");
        require(RECIPIENT.balance == recipientEthBefore, "failed sale credited recipient ETH");

        uint256 received = adapter.sellNativeActiveCurve(X, bought, quotedEth, RECIPIENT, deadline);
        require(received == quotedEth, "sale differs from preview");
        require(token.balanceOf(address(this)) == sourceXBefore - bought, "source X debit mismatch");
        require(token.balanceOf(CURVE) == curveXBefore + bought, "curve X credit mismatch");
        require(token.balanceOf(address(adapter)) == adapterXBefore, "adapter retained X");
        require(CURVE.balance == curveEthBefore - received, "curve ETH debit mismatch");
        require(RECIPIENT.balance == recipientEthBefore + received, "recipient ETH credit mismatch");
        require(address(adapter).balance == adapterEthBefore, "adapter retained ETH");
    }

    receive() external payable {}
}
