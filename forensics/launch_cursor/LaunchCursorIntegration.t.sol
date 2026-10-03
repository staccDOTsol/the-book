// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {LaunchCursorToken, IPonsV2LaunchFactoryCursor, ILaunchCursorConfigurator} from "./LaunchCursorToken.sol";
import {PositionInspector} from "./PositionInspector.sol";
import {StaticNextPoolFee} from "./StaticNextPoolFee.sol";

contract MockLaunchFactory {
    function getLaunchedToken(address token)
        external pure returns (IPonsV2LaunchFactoryCursor.LaunchedToken memory launch)
    {
        launch.token = token;
        launch.curve = address(0xC0FFEE);
        launch.phase = 0;
        launch.exists = true;
    }
}

contract MockCursorExecutor {
    address public controller;
    bool public configured;
    bool public active;
    bool public inBand;
    bool public atQuoteBoundary = true;
    bool public atTokenBoundary;
    bool public enteredBand;
    uint24 public openedFee;

    function bind(address controller_) external { controller = controller_; }

    function configureOpen(address, ILaunchCursorConfigurator.OpenConfig calldata config) external {
        require(msg.sender == controller);
        require(config.maxQuoteIn != 0);
        configured = true;
    }

    function open(address, uint24 feePips) external {
        require(msg.sender == controller && configured && !active);
        active = true;
        openedFee = feePips;
    }

    function exit(address) external returns (int32 netReturnBps) {
        require(msg.sender == controller && active && enteredBand && (atQuoteBoundary || atTokenBoundary));
        active = false;
        return 1_234;
    }

    function previewHarvest(address) external pure returns (uint256, uint256) {
        revert("no executable valuation");
    }

    function harvest(address) external pure { revert("no executable valuation"); }

    function setState(bool inBand_, bool atQuoteBoundary_, bool atTokenBoundary_) external {
        inBand = inBand_;
        atQuoteBoundary = atQuoteBoundary_;
        atTokenBoundary = atTokenBoundary_;
    }

    function inspect(address) external view returns (uint160, bool, bool, bool, bool) {
        require(active);
        return (uint160(1 << 96), inBand, atQuoteBoundary, atTokenBoundary, enteredBand);
    }

    function markEntered(address) external {
        require(active && (inBand || atTokenBoundary));
        enteredBand = true;
    }
}

contract LaunchCursorIntegrationTest {
    address private constant X = address(0xBEEF);

    MockLaunchFactory private factory;
    MockCursorExecutor private executor;
    PositionInspector private inspector;
    LaunchCursorToken private quote;

    constructor() {
        factory = new MockLaunchFactory();
        executor = new MockCursorExecutor();
        inspector = new PositionInspector(address(executor));
        address[] memory endpoints = new address[](0);
        quote = new LaunchCursorToken(
            "Quote", "Q", 1_000_000_000 ether,
            address(factory), address(executor), address(inspector),
            30, 500_000, 1 gwei, endpoints
        );
        executor.bind(address(quote));
        inspector.bindCursor(address(quote));
    }

    function _armAndOpen() private {
        quote.enqueue(X);
        ILaunchCursorConfigurator.OpenConfig memory config = ILaunchCursorConfigurator.OpenConfig({
            startingSqrtPriceX96: uint160(1 << 96),
            liquidity: 1,
            maxQuoteIn: 1 ether,
            tickSpacing: 1,
            tickLower: -1,
            tickUpper: 1,
            deadline: uint64(block.timestamp + 60)
        });
        quote.configureOpen(X, config);
        (bool attempted, bool succeeded) = quote.processNext();
        require(attempted && succeeded && executor.active(), "configured open failed");
    }

    function testEnqueueDoesNotPrematurelyOpen() external {
        quote.enqueue(X);
        (address token,,) = quote.nextAction();
        require(token == address(0), "unconfigured launch was armed");
        (bool attempted, bool succeeded) = quote.processNext();
        require(!attempted && !succeeded, "unconfigured launch attempted open");
    }

    function testFullRangeJumpQueuesExitAndRecordsFeeOutcome() external {
        _armAndOpen();
        uint24 fee = executor.openedFee();
        require(fee >= 50_000 && fee <= 500_000, "fee out of bounds");
        StaticNextPoolFee policy = quote.feePolicy();
        require(policy.selectedFee(X) == fee, "different fee selected");

        // The opening Q-only boundary is not an exit. One later observed jump
        // across the entire band proves entry and immediately queues exit.
        (bool entered, bool exitReady) = inspector.poke(X);
        require(!entered && !exitReady, "opened at exit-ready boundary");
        executor.setState(false, false, true);
        (entered, exitReady) = inspector.poke(X);
        require(entered && exitReady, "full crossing was missed");

        (address token, LaunchCursorToken.Step step,) = quote.nextAction();
        require(token == X && step == LaunchCursorToken.Step.Exit, "exit not prioritized");
        (bool attempted, bool succeeded) = quote.processNext();
        require(attempted && succeeded, "exit did not complete");
        (,, StaticNextPoolFee.Status status) = policy.assignments(X);
        require(status == StaticNextPoolFee.Status.Closed, "fee outcome not recorded");
    }

    function testInteriorThenReturnToQuoteBoundaryQueuesExit() external {
        _armAndOpen();
        executor.setState(true, false, false);
        (bool entered, bool exitReady) = inspector.poke(X);
        require(entered && !exitReady, "interior incorrectly exited");
        executor.setState(false, true, false);
        (entered, exitReady) = inspector.poke(X);
        require(entered && exitReady, "quote-side return not queued");
    }
}
