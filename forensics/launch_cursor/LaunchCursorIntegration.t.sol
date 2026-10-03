// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {
    LaunchCursorToken,
    IPonsV2LaunchFactoryCursor,
    ILaunchCursorConfigurator,
    ILaunchCursorExitConfigurator,
    ILaunchCursorHarvestConfigurator
} from "./LaunchCursorToken.sol";
import {PositionInspector} from "./PositionInspector.sol";
import {StaticNextPoolFee} from "./StaticNextPoolFee.sol";

interface VmOutcome {
    struct Log { bytes32[] topics; bytes data; address emitter; }
    function warp(uint256) external;
    function prank(address) external;
    function recordLogs() external;
    function getRecordedLogs() external returns (Log[] memory);
    function load(address target, bytes32 slot) external view returns (bytes32);
    function store(address target, bytes32 slot, bytes32 value) external;
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
    bool public exitTimed;
    bool public harvestConfigured;
    bool public comparableExit = true;
    bool public active;
    bool public inBand;
    bool public atQuoteBoundary = true;
    bool public atTokenBoundary;
    bool public enteredBand;
    bool public failOpen;
    bool public leaveUnusedMint;
    uint24 public openedFee;
    uint256[3] public lastMintedQuote;
    uint8 public openPositionCount = 1;
    uint8 public remainingPositions;

    function bind(address controller_) external { controller = controller_; }

    function configureOpen(address, bytes calldata encodedPlan) external {
        ILaunchCursorConfigurator.OpenConfig memory config =
            abi.decode(encodedPlan, (ILaunchCursorConfigurator.OpenConfig));
        require(msg.sender == controller);
        require(config.maxQuoteIn[0] != 0 && config.maxQuoteIn[1] != 0 && config.maxQuoteIn[2] != 0);
        configured = true;
    }

    function configureExit(address, bytes calldata encodedConfig) external {
        ILaunchCursorExitConfigurator.ExitConfig memory config =
            abi.decode(encodedConfig, (ILaunchCursorExitConfigurator.ExitConfig));
        require(msg.sender == controller && active &&
            (config.minTokenOut != 0 || config.minQuoteOut != 0) && config.deadline >= block.timestamp);
        exitConfigured = true;
        exitTimed = config.timed;
    }

    function configureHarvest(address, ILaunchCursorHarvestConfigurator.HarvestConfig calldata config) external {
        require(msg.sender == controller && active && config.grossEthValue > 0 &&
            config.estimatedGasUnits > 0 && config.deadline >= block.timestamp);
        harvestConfigured = true;
    }

    function setComparableExit(bool comparable_) external { comparableExit = comparable_; }
    function setFailOpen(bool fail_) external { failOpen = fail_; }
    function setLeaveUnusedMint(bool leave_) external { leaveUnusedMint = leave_; }
    function setOpenPositionCount(uint8 count) external {
        require(count > 0 && count <= 3);
        openPositionCount = count;
    }

    function activePositionCount(address) external view returns (uint8) { return remainingPositions; }

    function open(address, uint24 feePips, uint256[3] calldata mintedQuote)
        external returns (uint256[3] memory quoteSpent)
    {
        require(msg.sender == controller && configured && !active && !failOpen);
        LaunchCursorToken q = LaunchCursorToken(controller);
        for (uint8 i; i < 3; ++i) {
            require(mintedQuote[i] > 1 ether, "tranche mint too small");
            require(q.balanceOf(address(this)) >= mintedQuote[i], "open not funded by mint");
            lastMintedQuote[i] = mintedQuote[i];
            quoteSpent[i] = 1 ether;
            require(q.transfer(address(0xCAFE), quoteSpent[i]), "mock LP spend failed");
            if (!leaveUnusedMint) q.burn(mintedQuote[i] - quoteSpent[i]);
        }
        active = true;
        remainingPositions = openPositionCount;
        openedFee = feePips;
    }

    function exit(address) external returns (int32 netReturnBps, bool comparable) {
        require(msg.sender == controller && active && exitConfigured &&
            (exitTimed || (enteredBand && (atQuoteBoundary || atTokenBoundary))));
        --remainingPositions;
        active = remainingPositions != 0;
        exitConfigured = false;
        exitTimed = false;
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

    function inspectTranche(address, uint8 tranche) external view returns (uint160, bool, bool, bool, bool) {
        require(active && tranche < openPositionCount);
        return (uint160(1 << 96), inBand, atQuoteBoundary, atTokenBoundary, enteredBand);
    }

    function markEntered(address) external {
        require(active && (inBand || atTokenBoundary));
        enteredBand = true;
    }

    function markEnteredTranche(address, uint8 tranche) external {
        require(active && tranche < openPositionCount && (inBand || atTokenBoundary));
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
            LaunchCursorToken.Metadata("Quote", "Q", "Integration test quote", "ipfs://integration-test-q"),
            1_000_000_000 ether,
            address(factory), address(executor), address(inspector),
            30, 500_000, 1 gwei, endpoints
        );
        executor.bind(address(quote));
        inspector.bindCursor(address(quote));
    }

    function testTokenURIRetainsConstructorMetadata() external view {
        string memory expected = string.concat(
            "data:application/json;base64,",
            "eyJuYW1lIjoiUXVvdGUiLCJzeW1ib2wiOiJRIiwiZGVzY3JpcHRpb24iOiJJbnRlZ3JhdGlvbiB0ZXN0IHF1b3RlIiwiaW1hZ2UiOiJpcGZzOi8vaW50ZWdyYXRpb24tdGVzdC1xIn0="
        );
        require(keccak256(bytes(quote.tokenURI())) == keccak256(bytes(expected)), "wrong token metadata URI");
        require(quote.totalSupply() == quote.INITIAL_SUPPLY(), "metadata changed initial supply");
    }

    function testWizardUtf8ImageMetadataIsEmbeddedAtConstruction() external {
        address[] memory endpoints = new address[](0);
        LaunchCursorToken wizard = new LaunchCursorToken(
            LaunchCursorToken.Metadata(
                unicode"🧙‍♂️", unicode"🧙‍♂️", unicode"🧙‍♂️",
                "https://raw.githubusercontent.com/staccDOTsol/the-book/450d558b169408de1483c0540faa1aae72889a57/assets/wizard-token.png"
            ),
            1_000_000_000 ether, address(factory), address(executor), address(inspector),
            30, 500_000, 1 gwei, endpoints
        );
        string memory expected = string.concat(
            "data:application/json;base64,",
            "eyJuYW1lIjoi8J+nmeKAjeKZgu+4jyIsInN5bWJvbCI6IvCfp5nigI3imYLvuI8iLCJkZXNjcmlwdGlvbiI6IvCfp5nigI3imYLvuI8iLCJpbWFnZSI6Imh0dHBzOi8vcmF3LmdpdGh1YnVzZXJjb250ZW50LmNvbS9zdGFjY0RPVHNvbC90aGUtYm9vay80NTBkNTU4YjE2OTQwOGRlMTQ4M2MwNTQwZmFhMWFhZTcyODg5YTU3L2Fzc2V0cy93aXphcmQtdG9rZW4ucG5nIn0="
        );
        require(keccak256(bytes(wizard.tokenURI())) == keccak256(bytes(expected)),
            "wizard UTF-8 JSON or image missing from metadata");
        require(wizard.totalSupply() == wizard.INITIAL_SUPPLY(), "wrong initial supply");
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
            tranche: 0,
            minTokenOut: 1, minQuoteOut: 0, minEthOut: 1, minQOut: 1,
            deadline: uint64(block.timestamp + 60), timed: false
        });
        vm.prank(opening);
        (bool priceCanExit, bytes memory priceError) = address(quote).call(
            abi.encodeCall(quote.configureExit, (X, abi.encode(config)))
        );
        require(!priceCanExit && bytes4(priceError) == LaunchCursorToken.NotExitConfigurator.selector,
            "price signer configured exit");
        vm.prank(exiting);
        (bool exitReachedStage, bytes memory stageError) = address(quote).call(
            abi.encodeCall(quote.configureExit, (X, abi.encode(config)))
        );
        require(!exitReachedStage && bytes4(stageError) == LaunchCursorToken.BadLaunch.selector,
            "exit signer failed role check");
    }

    function _openPlan() private view returns (bytes memory) {
        ILaunchCursorConfigurator.OpenConfig memory config = ILaunchCursorConfigurator.OpenConfig({
            startingSqrtPriceX96: uint160(1 << 96),
            liquidity: [uint128(1), uint128(1), uint128(1)],
            maxQuoteIn: [uint128(1 ether), uint128(1 ether), uint128(1 ether)],
            tickSpacing: 1,
            tickLower: [int24(-1), int24(-1), int24(-1)],
            tickUpper: [int24(1), int24(1), int24(1)],
            deadline: uint64(block.timestamp + 60)
        });
        return abi.encode(config);
    }

    function _armAndOpen() private {
        quote.enqueue(X);
        quote.configureOpen(X, _openPlan());
        (bool attempted, bool succeeded) = quote.processNext();
        require(attempted && succeeded && executor.active(), "configured open failed");
    }

    function testOpenMintsPointOnePercentAndBurnsUnusedInOneAttempt() external {
        uint256 supplyBefore = quote.totalSupply();
        uint256 firstMint = supplyBefore / 1_000;
        uint256 secondMint = (supplyBefore + firstMint) / 1_000;
        uint256 thirdMint = (supplyBefore + firstMint + secondMint) / 1_000;
        _armAndOpen();
        require(quote.OPEN_MINT_BPS() == 10, "wrong mint fraction");
        require(quote.TOTAL_SUPPLY_CEILING() == 10 * quote.INITIAL_SUPPLY(), "wrong supply ceiling");
        require(executor.lastMintedQuote(0) == firstMint &&
            executor.lastMintedQuote(1) == secondMint &&
            executor.lastMintedQuote(2) == thirdMint, "mints did not compound in order");
        require(quote.totalSupply() == supplyBefore + 3 ether, "net supply must equal LP spend");
        require(quote.balanceOf(address(executor)) == 0, "unused mint remains in vault");
        (bool attempted, bool succeeded) = quote.processNext();
        require(!attempted && !succeeded && quote.totalSupply() == supplyBefore + 3 ether,
            "duplicate open inflated Q");
    }

    function testSupplyCeilingDefersOpenAndRetrySucceedsAfterHeadroomReturns() external {
        uint256 supplyBefore = quote.totalSupply();
        // CursorERC20 stores name, symbol, then totalSupply. Verify that
        // layout before using the cheatcode to model years of prior opens.
        bytes32 supplySlot = bytes32(uint256(2));
        require(uint256(vm.load(address(quote), supplySlot)) == supplyBefore, "unexpected supply slot");
        uint256 nearCeiling = quote.TOTAL_SUPPLY_CEILING() - 1 ether;
        vm.store(address(quote), supplySlot, bytes32(nearCeiling));
        quote.enqueue(X);
        quote.configureOpen(X, _openPlan());
        (bool attempted, bool succeeded) = quote.processNext();
        require(attempted && !succeeded, "ceiling did not defer opening");
        require(quote.totalSupply() == nearCeiling && quote.balanceOf(address(executor)) == 0,
            "ceiling failure changed balances");
        require(quote.pendingEntryCount() == 1, "ceiling failure lost queued launch");

        // A prior exit/burn frees issuance headroom; the same launch retries.
        vm.store(address(quote), supplySlot, bytes32(supplyBefore));
        vm.warp(block.timestamp + 30);
        (attempted, succeeded) = quote.processNext();
        require(attempted && succeeded, "opening did not retry after headroom returned");
        require(quote.totalSupply() == supplyBefore + 3 ether, "retry minted the wrong net amount");
    }

    function testOpenFailureRollsBackMintAndRetryIssuesOnlyOnce() external {
        uint256 supplyBefore = quote.totalSupply();
        executor.setFailOpen(true);
        quote.enqueue(X);
        quote.configureOpen(X, _openPlan());
        (bool attempted, bool succeeded) = quote.processNext();
        require(attempted && !succeeded, "forced failure did not fail");
        require(quote.totalSupply() == supplyBefore && quote.balanceOf(address(executor)) == 0,
            "failed open left newly minted Q");
        executor.setFailOpen(false);
        vm.warp(block.timestamp + 30);
        (attempted, succeeded) = quote.processNext();
        require(attempted && succeeded && quote.totalSupply() == supplyBefore + 3 ether,
            "retry did not mint exactly once");
        (attempted, succeeded) = quote.processNext();
        require(!attempted && !succeeded && quote.totalSupply() == supplyBefore + 3 ether,
            "repeat retry minted again");
    }

    function testOpenRejectsUnburnedMintAndCannotBeCalledDirectly() external {
        (bool direct,) = address(quote).call(
            abi.encodeCall(quote.executeOpenWithMint, (X, uint24(50_000)))
        );
        require(!direct, "owner opened an unqueued mint");
        uint256 supplyBefore = quote.totalSupply();
        executor.setLeaveUnusedMint(true);
        quote.enqueue(X);
        quote.configureOpen(X, _openPlan());
        (bool attempted, bool succeeded) = quote.processNext();
        require(attempted && !succeeded, "unburned mint was accepted");
        require(quote.totalSupply() == supplyBefore && quote.balanceOf(address(executor)) == 0 &&
            quote.balanceOf(address(0xCAFE)) == 0 && !executor.active(),
            "failed budget check did not revert whole open");
    }

    function testOnlyExitSignerCanBindActiveHarvest() external {
        ILaunchCursorHarvestConfigurator.HarvestConfig memory config =
            ILaunchCursorHarvestConfigurator.HarvestConfig({
                minTokenFee: 1, minQuoteFee: 0, minEthOut: 1, minQOut: 1,
                grossEthValue: 1 ether, estimatedGasUnits: 100_000,
                deadline: uint64(block.timestamp + 60)
            });
        (bool beforeOpen,) = address(quote).call(abi.encodeCall(quote.configureHarvest, (X, config)));
        require(!beforeOpen, "harvest bound before open");
        _armAndOpen();
        vm.prank(address(0xBAD));
        (bool unauthorized, bytes memory reason) = address(quote).call(
            abi.encodeCall(quote.configureHarvest, (X, config))
        );
        require(!unauthorized && bytes4(reason) == LaunchCursorToken.NotExitConfigurator.selector,
            "unauthorized harvest bound");
        quote.configureHarvest(X, config);
        require(executor.harvestConfigured(), "harvest bound not forwarded");
    }

    function testActivePositionCanQueueHarvestBeforeBandReport() external {
        _armAndOpen();
        vm.prank(address(inspector));
        require(quote.notifyHarvestReady(X), "active harvest report rejected");
        (address token, LaunchCursorToken.Step step,) = quote.nextAction();
        require(token == X && step == LaunchCursorToken.Step.Harvest, "harvest not queued");
    }

    function testHarvestRunsDuringExitRetryDelay() external {
        _armAndOpen();
        executor.setState(false, false, true);
        inspector.poke(X);
        vm.prank(address(inspector));
        require(quote.notifyHarvestReady(X), "boundary harvest report rejected");
        (address token, LaunchCursorToken.Step step,) = quote.nextAction();
        require(token == X && step == LaunchCursorToken.Step.Exit, "exit not prioritized");
        (bool attempted, bool succeeded) = quote.processNext();
        require(attempted && !succeeded, "unconfigured exit unexpectedly succeeded");
        (token, step,) = quote.nextAction();
        require(token == X && step == LaunchCursorToken.Step.Harvest,
            "harvest blocked during exit backoff");
    }

    function _configureExit(bool tokenSide) private {
        quote.configureExit(X, abi.encode(ILaunchCursorExitConfigurator.ExitConfig({
            tranche: 0,
            minTokenOut: tokenSide ? 1 : 0,
            minQuoteOut: tokenSide ? 0 : 1,
            minEthOut: 1,
            minQOut: 1,
            deadline: uint64(block.timestamp + 60), timed: false
        })));
    }

    function _configureTimedExit() private {
        quote.configureExit(X, abi.encode(ILaunchCursorExitConfigurator.ExitConfig({
            tranche: 0, minTokenOut: 0, minQuoteOut: 1,
            minEthOut: 0, minQOut: 0,
            deadline: uint64(block.timestamp + 60), timed: true
        })));
    }

    function testTimedExitFlagRequiresTwoHoursAfterOpen() external {
        _armAndOpen();
        executor.setState(false, false, true);
        inspector.poke(X);
        (bool early, bytes memory errorData) = address(quote).call(
            abi.encodeCall(quote.configureExit, (X, abi.encode(
                ILaunchCursorExitConfigurator.ExitConfig({
                    tranche: 0, minTokenOut: 1, minQuoteOut: 0,
                    minEthOut: 1, minQOut: 1,
                    deadline: uint64(block.timestamp + 60), timed: true
                })
            )))
        );
        require(!early && bytes4(errorData) == LaunchCursorToken.BadLaunch.selector,
            "timed exit configured before two hours");
        _configureExit(true);
    }

    function testUntouchedPoolWindDownRequeuesAllThreeExits() external {
        executor.setOpenPositionCount(3);
        _armAndOpen();
        (LaunchCursorToken.Stage stage,,, uint64 activeAt, uint64 enteredAt,,,,,,,) = quote.launches(X);
        require(stage == LaunchCursorToken.Stage.Active && enteredAt == 0,
            "untouched pool already entered");
        vm.warp(uint256(activeAt) + quote.WIND_DOWN_DELAY() - 1);
        (bool early,) = address(quote).call(abi.encodeCall(quote.requestWindDown, (X)));
        require(!early, "winddown opened early");
        vm.warp(uint256(activeAt) + quote.WIND_DOWN_DELAY());
        require(quote.requestWindDown(X), "winddown was not queued");
        require(!quote.requestWindDown(X) && quote.pendingExitCount() == 1,
            "duplicate winddown changed queue");
        bool exitReady;
        for (uint256 i; i < 3; ++i) {
            _configureTimedExit();
            (bool attempted, bool succeeded) = quote.processNext();
            require(attempted && succeeded, "timed tranche exit failed");
            (stage,,,, enteredAt,,, exitReady,,,,) = quote.launches(X);
            if (i < 2) {
                require(stage == LaunchCursorToken.Stage.Active && enteredAt == 0 &&
                    exitReady && quote.pendingExitCount() == 1,
                    "timed partial exit was not requeued");
            } else {
                require(stage == LaunchCursorToken.Stage.Exited && !exitReady &&
                    quote.pendingExitCount() == 0, "timed final exit did not close");
            }
        }
        (bool late,) = address(quote).call(abi.encodeCall(quote.requestWindDown, (X)));
        require(!late, "closed pool accepted winddown");
    }

    function testWindDownRetryBackoffCannotBeResetByDuplicateRequest() external {
        _armAndOpen();
        (,,, uint64 activeAt,,,,,,,,) = quote.launches(X);
        vm.warp(uint256(activeAt) + quote.WIND_DOWN_DELAY());
        require(quote.requestWindDown(X), "winddown was not queued");
        (bool attempted, bool succeeded) = quote.processNext();
        require(attempted && !succeeded, "unconfigured timed exit succeeded");
        (,,,,, uint64 nextActionAt,,,,,,) = quote.launches(X);
        require(nextActionAt > block.timestamp && !quote.requestWindDown(X),
            "duplicate winddown reset backoff");
        (,,,,, uint64 nextActionAfter,,,,,,) = quote.launches(X);
        require(nextActionAt == nextActionAfter, "retry timestamp changed");
        vm.warp(nextActionAt);
        _configureTimedExit();
        (attempted, succeeded) = quote.processNext();
        require(attempted && succeeded, "timed retry did not exit");
    }

    function testWindDownRejectsTimestampBeyondStoredRange() external {
        _armAndOpen();
        vm.warp(uint256(type(uint64).max) + 1);
        (bool accepted, bytes memory errorData) = address(quote).call(
            abi.encodeCall(quote.requestWindDown, (X))
        );
        require(!accepted && bytes4(errorData) == LaunchCursorToken.BadConfiguration.selector &&
            quote.pendingExitCount() == 0, "winddown timestamp wrapped");
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

    function testThreeTrancheExitsKeepLaunchActiveUntilFinalPosition() external {
        executor.setOpenPositionCount(3);
        _armAndOpen();
        executor.setState(false, false, true);
        for (uint256 i; i < 3; ++i) {
            inspector.poke(X);
            _configureExit(true);
            vm.recordLogs();
            (bool attempted, bool succeeded) = quote.processNext();
            require(attempted && succeeded, "tranche exit failed");
            VmOutcome.Log[] memory logs = vm.getRecordedLogs();
            uint256 finalMarkers;
            for (uint256 j; j < logs.length; ++j) {
                if (logs[j].emitter == address(quote) && logs[j].topics.length == 2 &&
                    logs[j].topics[0] == keccak256("AllPositionsExited(address)")) {
                    ++finalMarkers;
                    require(logs[j].topics[1] == bytes32(uint256(uint160(X))), "wrong final marker token");
                }
            }
            require(finalMarkers == (i == 2 ? 1 : 0), "final marker timing wrong");
            (LaunchCursorToken.Stage stage,,,,,,, bool exitReady,,,,) = quote.launches(X);
            if (i < 2) {
                require(stage == LaunchCursorToken.Stage.Active && !exitReady &&
                    quote.outcomeDeadline(X) == 0 && executor.activePositionCount(X) == 2 - i,
                    "partial exit finalized launch");
            } else {
                require(stage == LaunchCursorToken.Stage.Exited &&
                    executor.activePositionCount(X) == 0, "final tranche did not close launch");
            }
        }
        (,, StaticNextPoolFee.Status status) = quote.feePolicy().assignments(X);
        require(status == StaticNextPoolFee.Status.Closed, "fee feedback ran before final exit");
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
