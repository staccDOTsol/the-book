// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

/// @notice Read-only fork proof for the already deployed Q. Run at the pinned
/// block with `forge test --fork-url https://rpc.mainnet.chain.robinhood.com
/// --fork-block-number 79281000 --match-contract LiveQTransferTriggerForkTest`.
/// No transaction is broadcast to Robinhood.
interface ILiveQCursor {
    function transfer(address to, uint256 amount) external returns (bool);
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
    function approve(address spender, uint256 amount) external returns (bool);
    function automaticEnabled() external view returns (bool);
    function nextAction() external view returns (address token, uint8 step, uint64 eligibleAt);
}

interface ILiveQForkVm {
    function prank(address sender) external;
    function skip(bool skipTest) external;
}

contract LiveQTransferTriggerForkTest {
    ILiveQForkVm private constant vm =
        ILiveQForkVm(address(uint160(uint256(keccak256("hevm cheat code")))));
    ILiveQCursor private constant Q = ILiveQCursor(0x0E34d0792032Ffc54C058751cF048dA347193472);
    address private constant OWNER = 0x26E8134eCC3af5cCE32f34B03E7BD2f318B25158;
    address private constant MANAGER = 0x8366a39CC670B4001A1121B8F6A443A643e40951;
    address private constant QUEUED_X = 0x0eFfec14aC14c1518BF1f718Ee379B2781F7245C;
    address private constant ROUTER = address(0xCAFE);

    function _launchState() private view returns (uint8 stage, bool configured) {
        (bool ok, bytes memory result) = address(Q).staticcall(
            abi.encodeWithSignature("launches(address)", QUEUED_X)
        );
        require(ok && result.length >= 64, "launch read failed");
        assembly ("memory-safe") {
            stage := mload(add(result, 32))
            configured := iszero(iszero(mload(add(result, 64))))
        }
    }

    function _ready() private view {
        require(Q.automaticEnabled(), "automatic disabled");
        (address token, uint8 step,) = Q.nextAction();
        require(token == QUEUED_X && step == 0, "different queue root");
        (uint8 stage, bool configured) = _launchState();
        require(stage == 1 && configured, "root not configured");
    }

    function testOwnerTransferCanPayForQueuedOpen() external {
        vm.skip(block.chainid != 4663 || block.number != 79281000);
        _ready();
        vm.prank(OWNER);
        require(Q.transfer{gas: 12_000_000}(OWNER, 1), "self-transfer failed");
        (uint8 stage,) = _launchState();
        require(stage == 2, "owner-paid transfer did not open root");
    }

    function testRouterSettlementTransferDoesNotRunCursor() external {
        vm.skip(block.chainid != 4663 || block.number != 79281000);
        _ready();
        vm.prank(OWNER);
        require(Q.approve(ROUTER, 1), "approval failed");
        vm.prank(ROUTER);
        require(Q.transferFrom(OWNER, MANAGER, 1), "Q sell settlement failed");
        (uint8 stage,) = _launchState();
        require(stage == 1, "router transfer unexpectedly ran cursor");
    }
}
