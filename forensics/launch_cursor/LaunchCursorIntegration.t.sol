// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {
    LaunchCursorToken,
    IPonsV2LaunchFactoryCursor,
    ILaunchCursorConfigurator,
    ILaunchCursorExitConfigurator
} from "./LaunchCursorToken.sol";
import {PositionInspector} from "./PositionInspector.sol";
import {StaticNextPoolFee} from "./StaticNextPoolFee.sol";

interface VmOutcome {
    function warp(uint256) external;
    function prank(address) external;
}

contract MockLaunchFactory {
    mapping(address => uint8) public phase;

    function setPhase(address token, uint8 phase_) external { phase[token] = phase_; }

    function getLaunchedToken(address token)
        external view returns (IPonsV2LaunchFactoryCursor.LaunchedToken memory launch)
    {
        launch.token = token;
        launch.curve = address(0xC0FFEE);
        launch.phase = phase[token];
        launch.exists = true;
    }
}

contract MockCursorExecutor {
    address public controller;
    bool public configured;
    bool public exitConfigured;
    bool public comparableExit = true;
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

    function configureExit(address, ILaunchCursorExitConfigurator.ExitConfig calldata config) external {
        require(msg.sender == controller && active &&
            (config.minTokenOut != 0 || config.minQuoteOut != 0) && config.deadline >= block.timestamp);
        exitConfigured = true;
    }

    function setComparableExit(bool comparable_) external { comparableExit = comparable_; }

    function open(address, uint24 feePips) external {
        require(msg.sender == controller && configured && !active);
        active = true;
        openedFee = feePips;
    }

    function exit(address) external returns (int32 netReturnBps, bool comparable) {
        require(msg.sender == controller && active && enteredBand && exitConfigured && (atQuoteBoundary || atTokenBoundary));
        active = false;
        return (1_234, comparableExit);
    }

    function previewHarvest(address) external pure returns (uint256, uint256) {
        revert("no executable valuation");
    }

    function harvest(address) external pure { revert("no executable valuation"); }

    function emergencyUnwind(address, uint128, uint128, uint64, address)
        external returns (uint256 tokenAmount, uint256 quoteAmount)
    {
        require(msg.sender == controller && active);
        active = false;
        return (0, 1 ether);
    }

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
    VmOutcome private constant vm = VmOutcome(address(uint160(uint256(keccak256("hevm cheat code")))));

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

    function testExitSignerCannotShareOwnerOrPriceNonce() external {
        address opening = address(0xA11CE);
        address exiting = address(0xE417);
        quote.setAutomatic(false);
        (bool prematureActivation,) = address(quote).call(abi.encodeCall(quote.setAutomatic, (true)));
        require(!prematureActivation, "shared default roles activated");
        quote.setPriceConfigurator(opening);
        (bool sameOwner,) = address(quote).call(
            abi.encodeCall(quote.setExitConfigurator, (address(this)))
        );
        require(!sameOwner, "owner signer accepted as exit signer");
        (bool samePrice,) = address(quote).call(
            abi.encodeCall(quote.setExitConfigurator, (opening))
        );
        require(!samePrice, "price signer accepted as exit signer");
        quote.setExitConfigurator(exiting);
        require(quote.exitConfigurator() == exiting, "exit signer not assigned");
        (bool setPriceToExit,) = address(quote).call(
            abi.encodeCall(quote.setPriceConfigurator, (exiting))
        );
        require(!setPriceToExit, "exit signer accepted as price signer");
        quote.setAutomatic(true);
        (bool setPriceToOwner,) = address(quote).call(
            abi.encodeCall(quote.setPriceConfigurator, (address(this)))
        );
        require(!setPriceToOwner, "live owner signer accepted as price signer");
        ILaunchCursorExitConfigurator.ExitConfig memory config = ILaunchCursorExitConfigurator.ExitConfig({
            minTokenOut: 1, minQuoteOut: 0, minEthOut: 1, minQOut: 1,
            deadline: uint64(block.timestamp + 60)
        });
        vm.prank(opening);
        (bool priceCanExit, bytes memory priceError) = address(quote).call(
            abi.encodeCall(quote.configureExit, (X, config))
        );
        require(!priceCanExit && bytes4(priceError) == LaunchCursorToken.NotExitConfigurator.selector,
            "price signer configured exit");
        vm.prank(exiting);
        (bool exitReachedStage, bytes memory stageError) = address(quote).call(
            abi.encodeCall(quote.configureExit, (X, config))
        );
        require(!exitReachedStage && bytes4(stageError) == LaunchCursorToken.BadLaunch.selector,
            "exit signer failed role check");
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

    function _configureExit(bool tokenSide) private {
        quote.configureExit(X, ILaunchCursorExitConfigurator.ExitConfig({
            minTokenOut: tokenSide ? 1 : 0,
            minQuoteOut: tokenSide ? 0 : 1,
            minEthOut: 1,
            minQOut: 1,
            deadline: uint64(block.timestamp + 60)
        }));
    }

    function testEnqueueDoesNotPrematurelyOpen() external {
        quote.enqueue(X);
        (address token,,) = quote.nextAction();
        require(token == address(0), "unconfigured launch was armed");
        (bool attempted, bool succeeded) = quote.processNext();
        require(!attempted && !succeeded, "unconfigured launch attempted open");
    }

    function testFastGraduationMayStillEnqueue() external {
        factory.setPhase(X, 2);
        quote.enqueue(X);
        (LaunchCursorToken.Stage stage,,,,,,,,,,,) = quote.launches(X);
        require(stage == LaunchCursorToken.Stage.Queued, "graduated launch was missed");

        address swept = address(0xCAFE);
        factory.setPhase(swept, 1);
        quote.enqueue(swept);
        (stage,,,,,,,,,,,) = quote.launches(swept);
        require(stage == LaunchCursorToken.Stage.Queued, "swept launch was missed");
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
        _configureExit(true);
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

    function _unvaluedExit() private returns (uint64 deadline) {
        _armAndOpen();
        executor.setState(true, false, false);
        inspector.poke(X);
        executor.setState(false, true, false);
        inspector.poke(X);
        _configureExit(false);
        executor.setComparableExit(false);

        (bool attempted, bool succeeded) = quote.processNext();
        require(attempted && succeeded && !executor.active(), "unvalued exit did not finish");
        (LaunchCursorToken.Stage stage,,,,,,,,,,,) = quote.launches(X);
        require(stage == LaunchCursorToken.Stage.Exited, "launch did not exit");
        deadline = quote.outcomeDeadline(X);
        require(deadline == block.timestamp + quote.OUTCOME_REPORT_WINDOW(), "wrong evidence window");
        (,, StaticNextPoolFee.Status status) = quote.feePolicy().assignments(X);
        require(status == StaticNextPoolFee.Status.Selected, "unvalued exit prematurely scored");
        require(quote.feePolicy().totalClosed() == 0 && quote.feePolicy().totalCensored() == 0,
            "pending exit affected feedback counts");
    }

    function testUnvaluedExitCanReportOneReceiptBackedOutcome() external {
        _unvaluedExit();
        bytes32 evidence = keccak256("synthetic receipt-backed cash evidence");
        quote.reportExitedOutcome(X, -2_000, evidence);
        (,, StaticNextPoolFee.Status status) = quote.feePolicy().assignments(X);
        require(status == StaticNextPoolFee.Status.Closed, "reported exit not closed");
        require(quote.outcomeDeadline(X) == 0, "pending deadline not cleared");
        (uint64 closed,, int128 sum) = quote.feePolicy().armStats(
            uint8((executor.openedFee() - 50_000) / 10_000)
        );
        require(closed == 1 && sum == -2_000, "wrong cash return stored");
        try quote.reportExitedOutcome(X, 1, evidence) {
            revert("repeat outcome accepted");
        } catch {}
    }

    function testUnvaluedExitRejectsUnauthorizedOrUnsupportedReport() external {
        _unvaluedExit();
        bytes32 evidence = keccak256("synthetic evidence");
        vm.prank(address(0xBAD));
        try quote.reportExitedOutcome(X, 100, evidence) {
            revert("unauthorized report accepted");
        } catch {}
        try quote.reportExitedOutcome(X, 100, bytes32(0)) {
            revert("zero evidence hash accepted");
        } catch {}
        try quote.reportExitedOutcome(X, 10_001, evidence) {
            revert("out-of-range return accepted");
        } catch {}
        require(quote.outcomeDeadline(X) != 0, "rejected report cleared pending exit");
    }

    function testUnvaluedExitCensorsOnlyAfterFixedHorizon() external {
        uint64 deadline = _unvaluedExit();
        try quote.censorExpiredOutcome(X) {
            revert("early censor accepted");
        } catch {}
        vm.warp(deadline - 1);
        try quote.censorExpiredOutcome(X) {
            revert("pre-deadline censor accepted");
        } catch {}
        vm.warp(deadline);
        try quote.reportExitedOutcome(X, 100, keccak256("late evidence")) {
            revert("late report accepted");
        } catch {}
        quote.censorExpiredOutcome(X);
        (,, StaticNextPoolFee.Status status) = quote.feePolicy().assignments(X);
        require(status == StaticNextPoolFee.Status.Censored, "expired outcome not censored");
        require(quote.feePolicy().totalCensored() == 1, "wrong censored count");
        require(quote.outcomeDeadline(X) == 0, "expired deadline not cleared");
        try quote.censorExpiredOutcome(X) {
            revert("repeat censor accepted");
        } catch {}
    }

    function testEmergencyAbortRemovesStaleExitAndCensorsFee() external {
        _armAndOpen();
        executor.setState(false, false, true);
        inspector.poke(X);
        quote.emergencyAbort(X, 0, 0, uint64(block.timestamp + 60));
        require(!executor.active(), "position still active");
        (,, StaticNextPoolFee.Status status) = quote.feePolicy().assignments(X);
        require(status == StaticNextPoolFee.Status.Censored, "abort not recorded");
        (bool attempted, bool succeeded) = quote.processNext();
        require(attempted && succeeded, "stale exit not cleared");
        (address token,,) = quote.nextAction();
        require(token == address(0), "aborted exit still queued");
    }
}
