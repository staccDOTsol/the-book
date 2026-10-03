// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {StaticNextPoolFee} from "./StaticNextPoolFee.sol";

/// @notice The exact 15-field Pons V2 launch record ABI. This verifies a
/// supplied address; Pons does not expose an onchain iterator over launches.
interface IPonsV2LaunchFactoryCursor {
    struct LaunchedToken {
        address token;
        address curve;
        address deployer;
        address creatorFeeRecipient;
        address pairToken;
        uint256 graduationThreshold;
        uint24 poolFee;
        int24 tickSpacing;
        uint16 creatorTaxBps;
        bool buybackEnabled;
        uint8 phase;
        uint256 sweptQuote;
        uint256 sweptTokens;
        uint256 sweptAt;
        bool exists;
    }

    function getLaunchedToken(address token) external view returns (LaunchedToken memory);
}

/// @notice To be implemented and audited separately. It must custody the quote
/// tokens and LP positions, read an executable launch/quote price, settle v4
/// deltas, and burn recovered quote tokens if that is the configured policy.
/// `open` must initialize and fund its configured position(s) atomically,
/// with the LP owned by the executor/vault. Its implementation must restrict
/// callers to this cursor, accept/hold the LP asset, handle ETH/Permit2/v4
/// settlement, and revert on partial or no-op operations. This token does
/// none of those operations; a no-op executor advances its cursor unsafely.
interface ILaunchCursorExecutor {
    function open(address launchToken, uint24 feePips, uint256[3] calldata mintedQuote)
        external returns (uint256[3] memory quoteSpent);
    function activePositionCount(address launchToken) external view returns (uint256);
    /// @notice Must finish LP withdrawal, liquidation, burns, and payouts.
    /// A completed but unvalued exit remains pending for receipt-backed cash
    /// accounting, then is censored after the fixed reporting window if no
    /// comparable return can be proved.
    function exit(address launchToken) external returns (int32 netReturnBps, bool comparable);
    /// @notice Return claimable fees expressed as gross ETH value and the gas
    /// units expected for this cursor's whole harvest transaction.
    function previewHarvest(address launchToken) external view returns (uint256 grossEthValue, uint256 estimatedGasUnits);
    /// @notice Claim fees atomically; revert if nothing was claimed.
    function harvest(address launchToken) external;
    function emergencyUnwind(
        address launchToken, uint128 minTokenOut, uint128 minQuoteOut, uint64 deadline, address recipient
    ) external returns (uint256 tokenAmount, uint256 quoteAmount);
    function rescueHeldERC20(address token, uint256 amount, address recipient) external;
}

/// @notice The price keeper supplies a short-lived, independently verified
/// three-position quote-only configuration. The executor is bound to this Q token as its
/// controller, so this forwarder is the only normal route to configure it.
interface ILaunchCursorConfigurator {
    struct OpenConfig {
        uint160 startingSqrtPriceX96;
        uint128[3] liquidity;
        uint128[3] maxQuoteIn;
        int24 tickSpacing;
        int24[3] tickLower;
        int24[3] tickUpper;
        uint64 deadline;
    }

    function configureOpen(address token, bytes calldata encodedPlan) external;
}

interface ILaunchCursorExitConfigurator {
    struct ExitConfig {
        uint8 tranche;
        uint128 minTokenOut;
        uint128 minQuoteOut;
        uint256 minEthOut;
        uint256 minQOut;
        uint64 deadline;
        bool timed;
    }

    function configureExit(address token, bytes calldata encodedConfig) external;
}

interface ILaunchCursorHarvestConfigurator {
    struct HarvestConfig {
        uint128 minTokenFee;
        uint128 minQuoteFee;
        uint256 minEthOut;
        uint256 minQOut;
        uint256 grossEthValue;
        uint32 estimatedGasUnits;
        uint64 deadline;
    }

    function configureHarvest(address token, HarvestConfig calldata config) external;
}

/// @notice Small standalone ERC-20 base with the OpenZeppelin 5-style `_update`
/// extension point. No nonstandard transfer fees or balance changes are used.
abstract contract CursorERC20 {
    string public name;
    string public symbol;
    uint8 public constant decimals = 18;
    uint256 public totalSupply;

    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    event Transfer(address indexed from, address indexed to, uint256 amount);
    event Approval(address indexed owner, address indexed spender, uint256 amount);

    error InvalidAddress();
    error InsufficientBalance();
    error InsufficientAllowance();

    constructor(string memory name_, string memory symbol_) {
        name = name_;
        symbol = symbol_;
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        if (spender == address(0)) revert InvalidAddress();
        allowance[msg.sender][spender] = amount;
        emit Approval(msg.sender, spender, amount);
        return true;
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        if (to == address(0)) revert InvalidAddress();
        _update(msg.sender, to, amount);
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        if (from == address(0) || to == address(0)) revert InvalidAddress();
        uint256 permitted = allowance[from][msg.sender];
        if (permitted != type(uint256).max) {
            if (permitted < amount) revert InsufficientAllowance();
            unchecked {
                allowance[from][msg.sender] = permitted - amount;
            }
            emit Approval(from, msg.sender, allowance[from][msg.sender]);
        }
        _update(from, to, amount);
        return true;
    }

    function burn(uint256 amount) external {
        _update(msg.sender, address(0), amount);
    }

    function _update(address from, address to, uint256 amount) internal virtual {
        if (from == address(0) && to == address(0)) revert InvalidAddress();
        if (from == address(0)) {
            totalSupply += amount;
        } else {
            uint256 held = balanceOf[from];
            if (held < amount) revert InsufficientBalance();
            unchecked {
                balanceOf[from] = held - amount;
            }
        }
        if (to == address(0)) totalSupply -= amount;
        else balanceOf[to] += amount;
        emit Transfer(from, to, amount);
    }
}

/// @notice Prototype transfer-triggered cursor. A watcher owned by `owner`
/// calls enqueue after observing Pons TokenLaunched logs. Each eligible
/// direct EOA transfer attempts at most one action; anyone can also processNext.
/// It is deliberately not a deployable LP system without the executor.
contract LaunchCursorToken is CursorERC20 {
    struct Metadata {
        string name;
        string symbol;
        string description;
        string imageURI;
    }

    enum Stage {
        None,
        Queued,
        Active,
        Exited,
        Skipped,
        Aborted
    }

    enum Step {
        Open,
        Exit,
        Harvest
    }

    struct Launch {
        Stage stage;
        bool openConfigured;
        uint64 queuedAt;
        uint64 activeAt;
        uint64 enteredBandAt;
        uint64 nextActionAt;
        uint32 failures;
        bool exitReady;
        bool quoteBoundary;
        uint64 nextHarvestAt;
        uint32 harvestFailures;
        bool harvestReady;
    }

    struct PendingReport {
        uint64 bandAt;
        bool exit;
        bool quoteBoundary;
        bool harvest;
        bool queued;
    }

    struct SelectedAction {
        address token;
        Step step;
        uint8 source; // 0=fresh entry, 1=entry retry, 2=exit, 3=fresh harvest, 4=harvest retry.
    }

    address public immutable owner;
    /// @notice Fixed at construction so the launch image cannot be changed later.
    string private _metadataURI;
    address public priceConfigurator;
    address public exitConfigurator;
    IPonsV2LaunchFactoryCursor public immutable ponsFactory;
    ILaunchCursorExecutor public immutable executor;
    StaticNextPoolFee public immutable feePolicy;
    address public immutable exitNotifier;
    uint64 public immutable retryDelaySeconds;

    uint256 private constant POST_CALL_GAS_RESERVE = 150_000;
    uint256 private constant HARVEST_PREVIEW_GAS_LIMIT = 100_000;
    uint256 private constant TRANSFER_STEP_OVERHEAD = 180_000;
    uint256 private constant OUTER_TRANSFER_GAS_RESERVE = 50_000;
    uint256 private constant HARVEST_GAS_VALUE_MULTIPLIER = 2;
    uint64 public constant OUTCOME_REPORT_WINDOW = 7 days;
    uint256 public constant WIND_DOWN_DELAY = 120 minutes;
    uint256 public constant INITIAL_SUPPLY = 1_000_000_000 ether;
    uint16 public constant OPEN_MINT_BPS = 100;
    uint32 public transferStepGasLimit;
    uint256 public harvestGasPriceCeilingWei;
    bool public automaticEnabled = true;
    bool private _processing;
    uint8 private _opensSinceHarvest;
    address private _processingToken;
    Step private _processingStep;

    mapping(address => Launch) public launches;
    /// @notice Unvalued exits await receipt-backed cash accounting until this
    /// timestamp. Zero means no outcome is pending or it was finalized.
    mapping(address => uint64) public outcomeDeadline;
    mapping(address => PendingReport) private _pendingByToken;
    mapping(address => bool) public internalEndpoint;

    // Fresh entries are a stack (LIFO). Failed entries wait in a retry heap.
    // Only boundary-notified positions enter the exit min-heap. Ready exits
    // outrank entry work; failed exits wait until their retry timestamp.
    address[] private _entries;
    address[] private _entryRetries;
    address[] private _exits;
    // Ready harvests are a stack; deferred/failed harvests use a separate
    // time-ordered heap. An exit leaves its harvest entry for lazy removal.
    address[] private _harvests;
    address[] private _harvestRetries;
    // Notifier callbacks during executor work only append here. One report is
    // applied before the next scheduler selection, after the old roots settle.
    mapping(uint256 => address) private _pendingReports;
    uint256 private _pendingReportHead;
    uint256 private _pendingReportTail;

    event LaunchEnqueued(address indexed token, address indexed curve);
    event StepSucceeded(address indexed token, Step step);
    event AllPositionsExited(address indexed token);
    event OpenMinted(address indexed token, uint256 mintedQuote, uint256 quoteSpent);
    event OpenTrancheMinted(address indexed token, uint8 indexed tranche, uint256 mintedQuote, uint256 quoteSpent);
    event StepFailed(address indexed token, Step step, uint32 failures, uint64 nextAttemptAt);
    event BandEntered(address indexed token);
    event ExitReady(address indexed token, bool allQuote);
    event WindDownRequested(address indexed token);
    event HarvestReady(address indexed token);
    event HarvestDeferred(address indexed token, uint256 grossEthValue, uint256 estimatedGasUnits, uint64 nextAttemptAt);
    event ReportDeferred(address indexed token);
    event StaleHarvestRemoved(address indexed token);
    event StaleExitRemoved(address indexed token);
    event LaunchSkipped(address indexed token);
    event LaunchAborted(address indexed token, uint256 tokenRecovered, uint256 quoteRecovered);
    event HeldAssetRescued(address indexed token, uint256 amount);
    event PriceConfiguratorSet(address indexed configurator);
    event ExitConfiguratorSet(address indexed configurator);
    event OpenPriceConfigured(address indexed token, bytes32 indexed planHash);
    event ExitBoundConfigured(address indexed token, bytes32 indexed configHash);
    event HarvestBoundConfigured(address indexed token);
    event SkippedEntryRemoved(address indexed token);
    event InternalEndpointSet(address indexed endpoint, bool internalCall);
    event AutomaticSet(bool enabled);
    event TransferStepGasLimitSet(uint32 gasLimit);
    event HarvestGasPriceCeilingSet(uint256 gasPriceWei);
    event FeeOutcomePending(address indexed token, uint64 deadline);
    event FeeOutcomeReported(address indexed token, int32 netReturnBps, bytes32 evidenceHash);
    event FeeOutcomeCensored(address indexed token);

    error NotOwner();
    error NotPriceConfigurator();
    error NotExitConfigurator();
    error Busy();
    error BadLaunch();
    error AlreadyEnqueued();
    error BadConfiguration();
    error NotNotifier();
    error BadOutcome();

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    modifier onlyPriceConfigurator() {
        if (msg.sender != priceConfigurator) revert NotPriceConfigurator();
        _;
    }

    modifier onlyExitConfigurator() {
        if (msg.sender != exitConfigurator) revert NotExitConfigurator();
        _;
    }

    modifier whenIdle() {
        if (_processing) revert Busy();
        _;
    }

    modifier onlyNotifier() {
        if (msg.sender != exitNotifier) revert NotNotifier();
        _;
    }

    constructor(
        Metadata memory metadata_,
        uint256 initialSupply_,
        address factory_,
        address executor_,
        address exitNotifier_,
        uint64 retryDelaySeconds_,
        uint32 transferStepGasLimit_,
        uint256 harvestGasPriceCeilingWei_,
        address[] memory settlementEndpoints_
    ) CursorERC20(metadata_.name, metadata_.symbol) {
        if (
            factory_.code.length == 0 || executor_.code.length == 0 ||
            exitNotifier_.code.length == 0 || retryDelaySeconds_ == 0 ||
            bytes(metadata_.description).length == 0 || bytes(metadata_.imageURI).length == 0 ||
            initialSupply_ != INITIAL_SUPPLY ||
            transferStepGasLimit_ < 100_000 || transferStepGasLimit_ > 10_000_000 ||
            harvestGasPriceCeilingWei_ == 0
        ) revert BadConfiguration();
        _metadataURI = _buildTokenURI(
            metadata_.name, metadata_.symbol, metadata_.description, metadata_.imageURI
        );
        owner = msg.sender;
        priceConfigurator = msg.sender;
        exitConfigurator = msg.sender;
        ponsFactory = IPonsV2LaunchFactoryCursor(factory_);
        executor = ILaunchCursorExecutor(executor_);
        feePolicy = new StaticNextPoolFee(address(this));
        exitNotifier = exitNotifier_;
        retryDelaySeconds = retryDelaySeconds_;
        transferStepGasLimit = transferStepGasLimit_;
        harvestGasPriceCeilingWei = harvestGasPriceCeilingWei_;
        internalEndpoint[address(this)] = true;
        internalEndpoint[executor_] = true;
        internalEndpoint[exitNotifier_] = true;
        for (uint256 i; i < settlementEndpoints_.length; ++i) {
            if (settlementEndpoints_[i] == address(0)) revert BadConfiguration();
            internalEndpoint[settlementEndpoints_[i]] = true;
            emit InternalEndpointSet(settlementEndpoints_[i], true);
        }
        _update(address(0), msg.sender, initialSupply_);
    }

    /// @notice Uniswap-compatible ERC-20 metadata URI with an embedded JSON document.
    function tokenURI() external view returns (string memory) {
        return _metadataURI;
    }

    /// @dev A reverting self-call makes issuance and the entire executor open
    /// one atomic attempt, even though the scheduler catches open failures.
    function executeOpenWithMint(address token, uint24 feePips) external {
        if (
            msg.sender != address(this) || !_processing ||
            _processingStep != Step.Open || _processingToken != token ||
            launches[token].stage != Stage.Queued || !launches[token].openConfigured
        ) revert BadConfiguration();
        uint256 supplyBefore = totalSupply;
        uint256 executorBalanceBefore = balanceOf[address(executor)];
        uint256[3] memory mintedQuote;
        uint256 totalMinted;
        for (uint8 i; i < 3; ++i) {
            // Each mint sees supply after the prior mint. The executor burns
            // every tranche's unused amount before this call completes.
            uint256 amount = totalSupply * OPEN_MINT_BPS / 10_000;
            if (amount == 0) revert BadConfiguration();
            mintedQuote[i] = amount;
            totalMinted += amount;
            _update(address(0), address(executor), amount);
        }
        uint256[3] memory quoteSpent = executor.open(token, feePips, mintedQuote);
        uint256 totalSpent;
        for (uint8 i; i < 3; ++i) {
            uint256 spent = quoteSpent[i];
            if (spent == 0 || spent > mintedQuote[i]) revert BadConfiguration();
            totalSpent += spent;
            emit OpenTrancheMinted(token, i, mintedQuote[i], spent);
        }
        if (
            balanceOf[address(executor)] != executorBalanceBefore ||
            totalSupply != supplyBefore + totalSpent
        ) revert BadConfiguration();
        emit OpenMinted(token, totalMinted, totalSpent);
    }

    function _buildTokenURI(
        string memory name_, string memory symbol_, string memory description_, string memory imageURI_
    ) private pure returns (string memory) {
        bytes memory json = abi.encodePacked(
            '{"name":"', _jsonEscape(name_),
            '","symbol":"', _jsonEscape(symbol_),
            '","description":"', _jsonEscape(description_),
            '","image":"', _jsonEscape(imageURI_), '"}'
        );
        return string.concat("data:application/json;base64,", _base64(json));
    }

    function _jsonEscape(string memory value) private pure returns (string memory) {
        bytes memory input = bytes(value);
        uint256 outputLength;
        for (uint256 i; i < input.length; ++i) {
            uint8 c = uint8(input[i]);
            outputLength += c < 0x20 ? 6 : (c == 0x22 || c == 0x5c ? 2 : 1);
        }
        bytes memory output = new bytes(outputLength);
        bytes16 hexDigits = "0123456789abcdef";
        uint256 j;
        for (uint256 i; i < input.length; ++i) {
            uint8 c = uint8(input[i]);
            if (c < 0x20) {
                output[j++] = 0x5c;
                output[j++] = 0x75;
                output[j++] = 0x30;
                output[j++] = 0x30;
                output[j++] = hexDigits[c >> 4];
                output[j++] = hexDigits[c & 0x0f];
            } else if (c == 0x22 || c == 0x5c) {
                output[j++] = 0x5c;
                output[j++] = bytes1(c);
            } else {
                output[j++] = bytes1(c);
            }
        }
        return string(output);
    }

    function _base64(bytes memory input) private pure returns (string memory) {
        bytes memory alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
        bytes memory output = new bytes(4 * ((input.length + 2) / 3));
        uint256 j;
        for (uint256 i; i < input.length; i += 3) {
            uint256 chunk = uint256(uint8(input[i])) << 16;
            if (i + 1 < input.length) chunk |= uint256(uint8(input[i + 1])) << 8;
            if (i + 2 < input.length) chunk |= uint256(uint8(input[i + 2]));
            output[j++] = alphabet[(chunk >> 18) & 0x3f];
            output[j++] = alphabet[(chunk >> 12) & 0x3f];
            output[j++] = i + 1 < input.length ? alphabet[(chunk >> 6) & 0x3f] : bytes1(0x3d);
            output[j++] = i + 2 < input.length ? alphabet[chunk & 0x3f] : bytes1(0x3d);
        }
        return string(output);
    }

    /// @dev The automated owner watcher only enqueues. This contract cannot
    /// read past TokenLaunched logs or enumerate a Pons mapping by itself.
    function enqueue(address token) external onlyOwner whenIdle {
        if (token == address(0)) revert BadLaunch();
        if (launches[token].stage != Stage.None) revert AlreadyEnqueued();
        IPonsV2LaunchFactoryCursor.LaunchedToken memory record = ponsFactory.getLaunchedToken(token);
        // The watcher may submit after a fast launch has graduated. Preserve
        // its verified factory identity; the price keeper decides when its
        // current phase has an executable ETH route.
        if (!record.exists || record.token != token || record.curve == address(0) || record.phase > 2) {
            revert BadLaunch();
        }
        Launch storage launch = launches[token];
        launch.stage = Stage.Queued;
        launch.queuedAt = uint64(block.timestamp);
        emit LaunchEnqueued(token, record.curve);
    }

    /// @notice Exceptional owner maintenance for an entry that has gone stale.
    /// Queue removal is lazy so this call never scans an unbounded list.
    function skipStale(address token) external onlyOwner whenIdle {
        Launch storage launch = launches[token];
        if (launch.stage != Stage.Queued) revert BadLaunch();
        if (feePolicy.selectedFee(token) != 0) {
            feePolicy.recordCensored(token, StaticNextPoolFee.CensorReason.NeverOpened);
        }
        launch.stage = Stage.Skipped;
        emit LaunchSkipped(token);
    }

    /// @notice Explicit emergency recovery if the normal exit is unavailable.
    /// The LP may be burned at any price; receipts go to the owner for manual
    /// handling rather than through the automatic burn/fanout distribution.
    function emergencyAbort(address token, uint128 minTokenOut, uint128 minQuoteOut, uint64 deadline)
        external onlyOwner whenIdle
    {
        Launch storage launch = launches[token];
        if (launch.stage != Stage.Active) revert BadLaunch();
        (uint256 tokenRecovered, uint256 quoteRecovered) = ILaunchCursorExecutor(address(executor))
            .emergencyUnwind(token, minTokenOut, minQuoteOut, deadline, owner);
        feePolicy.recordCensored(token, StaticNextPoolFee.CensorReason.StuckUnwound);
        launch.stage = Stage.Aborted;
        launch.exitReady = false;
        launch.harvestReady = false;
        emit LaunchAborted(token, tokenRecovered, quoteRecovered);
    }

    /// @notice Return unused Q prefunding and previously collected ERC20 fees
    /// to the owner. LP principal remains in the PositionManager until a
    /// normal exit or emergency abort burns the NFT.
    function rescueHeldERC20(address token, uint256 amount) external onlyOwner whenIdle {
        if (token == address(0) || amount == 0) revert BadConfiguration();
        executor.rescueHeldERC20(token, amount, owner);
        emit HeldAssetRescued(token, amount);
    }

    /// @notice A separate automated price keeper may be assigned this role.
    /// The owner-controlled launch watcher can continue to write only enqueue.
    function setPriceConfigurator(address configurator) external onlyOwner whenIdle {
        if (configurator == address(0) ||
            (automaticEnabled && configurator == owner) ||
            (exitConfigurator != owner && configurator == exitConfigurator)) {
            revert BadConfiguration();
        }
        priceConfigurator = configurator;
        emit PriceConfiguratorSet(configurator);
    }

    /// @notice The exit signer has its own nonce stream, independent of the
    /// owner watcher and opening price keeper. Fund it separately for gas.
    function setExitConfigurator(address configurator) external onlyOwner whenIdle {
        if (configurator == address(0) || configurator == owner || configurator == priceConfigurator) {
            revert BadConfiguration();
        }
        exitConfigurator = configurator;
        emit ExitConfiguratorSet(configurator);
    }

    /// @notice Commits a short-lived price, three ranges, liquidity and spend
    /// caps. Each cap is checked against its own 1%-of-current-supply mint
    /// during the later atomic open attempt.
    function configureOpen(address token, bytes calldata encodedPlan)
        external onlyPriceConfigurator whenIdle
    {
        Launch storage launch = launches[token];
        if (launch.stage != Stage.Queued || encodedPlan.length == 0) revert BadLaunch();
        ILaunchCursorConfigurator(address(executor)).configureOpen(token, encodedPlan);
        if (!launch.openConfigured) {
            launch.openConfigured = true;
            _entries.push(token);
        }
        emit OpenPriceConfigured(token, keccak256(encodedPlan));
    }

    /// @notice Commit short-lived LP and swap minima for a queued boundary or
    /// timed exit. Timed configurations are unavailable before open+120m.
    function configureExit(address token, bytes calldata encodedConfig)
        external onlyExitConfigurator whenIdle
    {
        Launch storage launch = launches[token];
        if (launch.stage != Stage.Active || !launch.exitReady || encodedConfig.length == 0) revert BadLaunch();
        ILaunchCursorExitConfigurator.ExitConfig memory config =
            abi.decode(encodedConfig, (ILaunchCursorExitConfigurator.ExitConfig));
        if (config.timed && block.timestamp < uint256(launch.activeAt) + WIND_DOWN_DELAY) revert BadLaunch();
        ILaunchCursorExitConfigurator(address(executor)).configureExit(token, encodedConfig);
        emit ExitBoundConfigured(token, keccak256(encodedConfig));
    }

    /// @notice After two hours, anyone may queue liquidation of an active
    /// launch even if none of its three bands was ever entered.
    function requestWindDown(address token) external whenIdle returns (bool queued) {
        Launch storage launch = launches[token];
        if (launch.stage != Stage.Active || block.timestamp < uint256(launch.activeAt) + WIND_DOWN_DELAY) {
            revert BadLaunch();
        }
        if (launch.exitReady || _pendingByToken[token].exit) return false;
        if (block.timestamp > type(uint64).max) revert BadConfiguration();
        launch.exitReady = true;
        launch.quoteBoundary = false;
        launch.nextActionAt = uint64(block.timestamp);
        _heapPush(_exits, token, false);
        emit WindDownRequested(token);
        return true;
    }

    /// @notice The exit signer supplies a bounded interim fee claim for an
    /// active position. The executor validates the position and live minima.
    function configureHarvest(address token, ILaunchCursorHarvestConfigurator.HarvestConfig calldata config)
        external onlyExitConfigurator whenIdle
    {
        if (launches[token].stage != Stage.Active) revert BadLaunch();
        ILaunchCursorHarvestConfigurator(address(executor)).configureHarvest(token, config);
        emit HarvestBoundConfigured(token);
    }

    /// @notice Record a receipt-backed gross mark-to-market ETH-equivalent
    /// estimate after the final tranche exits. The price configurator checks
    /// recipient ETH, Q burned at exits and interim harvests, and net Q minted
    /// into the three LPs against historical executable Q/ETH quotes. Gas is
    /// excluded. This contract cannot verify that offchain valuation itself.
    function reportExitedOutcome(address token, int32 netReturnBps, bytes32 evidenceHash)
        external onlyPriceConfigurator whenIdle
    {
        uint64 deadline = outcomeDeadline[token];
        if (
            launches[token].stage != Stage.Exited || deadline == 0 ||
            block.timestamp >= deadline || evidenceHash == bytes32(0)
        ) revert BadOutcome();
        (,, StaticNextPoolFee.Status status) = feePolicy.assignments(token);
        if (status != StaticNextPoolFee.Status.Selected) revert BadOutcome();
        feePolicy.recordClosed(token, netReturnBps);
        delete outcomeDeadline[token];
        emit FeeOutcomeReported(token, netReturnBps, evidenceHash);
    }

    /// @notice After the fixed evidence window, finalize a completed exit
    /// whose receipts or historical Q/ETH valuation could not be proven.
    /// It remains counted as censored, never as a zero-return closed pool.
    function censorExpiredOutcome(address token) external onlyPriceConfigurator whenIdle {
        uint64 deadline = outcomeDeadline[token];
        if (
            launches[token].stage != Stage.Exited || deadline == 0 ||
            block.timestamp < deadline
        ) revert BadOutcome();
        (,, StaticNextPoolFee.Status status) = feePolicy.assignments(token);
        if (status != StaticNextPoolFee.Status.Selected) revert BadOutcome();
        feePolicy.recordCensored(token, StaticNextPoolFee.CensorReason.UnvaluedExit);
        delete outcomeDeadline[token];
        emit FeeOutcomeCensored(token);
    }

    /// @notice The authenticated position inspector reports that the open
    /// position has entered its configured range. Reports made during an
    /// executor call are buffered until the next scheduler selection.
    function notifyBandEntered(address token) external onlyNotifier returns (bool recorded) {
        Launch storage launch = launches[token];
        PendingReport storage pending = _pendingByToken[token];
        if (!_reportable(launch, token) || launch.enteredBandAt != 0 || pending.bandAt != 0) return false;
        if (_processing || pending.queued) {
            pending.bandAt = uint64(block.timestamp);
            _queuePendingReport(token, pending);
            return true;
        }
        launch.enteredBandAt = uint64(block.timestamp);
        emit BandEntered(token);
        return true;
    }

    /// @notice Report the first one-sided boundary reached *after* entry.
    /// `allQuote=true` denotes Q only; false denotes launch token only. The
    /// notifier must verify this from its own position/tick state. Separately,
    /// anyone may request winddown at open+120m without entry. Repeated
    /// reports never reset retry backoff.
    function notifyExitReady(address token, bool allQuote) external onlyNotifier returns (bool recorded) {
        Launch storage launch = launches[token];
        PendingReport storage pending = _pendingByToken[token];
        if (
            !_reportable(launch, token) ||
            (launch.enteredBandAt == 0 && pending.bandAt == 0) ||
            launch.exitReady || pending.exit
        ) return false;
        if (_processing || pending.queued) {
            pending.exit = true;
            pending.quoteBoundary = allQuote;
            _queuePendingReport(token, pending);
            return true;
        }
        launch.exitReady = true;
        launch.quoteBoundary = allQuote;
        launch.nextActionAt = uint64(block.timestamp);
        _heapPush(_exits, token, false);
        emit ExitReady(token, allQuote);
        return true;
    }

    /// @notice Authenticated position inspector reports claimable LP fees.
    /// Repeated reports do not duplicate queue entries or reset retry time.
    function notifyHarvestReady(address token) external onlyNotifier returns (bool recorded) {
        Launch storage launch = launches[token];
        PendingReport storage pending = _pendingByToken[token];
        bool rearmingCurrentHarvest = _processing && _processingToken == token && _processingStep == Step.Harvest;
        if (
            launch.stage != Stage.Active ||
            (launch.harvestReady && !rearmingCurrentHarvest) || pending.harvest
        ) return false;
        if (_processing || pending.queued) {
            pending.harvest = true;
            _queuePendingReport(token, pending);
            return true;
        }
        launch.harvestReady = true;
        _harvests.push(token);
        emit HarvestReady(token);
        return true;
    }

    /// @notice Permissionless fallback when no eligible quote-token transfer
    /// happens or a transfer supplies too little gas for its automatic step.
    function processNext() external returns (bool attempted, bool succeeded) {
        if (_processing) revert Busy();
        return _processNext();
    }

    /// @dev Only `this` may enter this bounded subcall. If any scheduler code,
    /// including heap bookkeeping after the executor call, runs out of gas,
    /// the whole subcall reverts and the parent ERC-20 transfer still succeeds.
    function processTransferStep() external returns (bool attempted, bool succeeded) {
        if (msg.sender != address(this)) revert NotOwner();
        if (_processing) revert Busy();
        return _processNext();
    }

    function setInternalEndpoint(address endpoint, bool internalCall) external onlyOwner whenIdle {
        if (
            endpoint == address(0) || endpoint == address(this) ||
            endpoint == address(executor) || endpoint == exitNotifier
        ) {
            revert BadConfiguration();
        }
        internalEndpoint[endpoint] = internalCall;
        emit InternalEndpointSet(endpoint, internalCall);
    }

    function setAutomatic(bool enabled) external onlyOwner whenIdle {
        if (enabled &&
            (priceConfigurator == owner || exitConfigurator == owner || priceConfigurator == exitConfigurator)) {
            revert BadConfiguration();
        }
        automaticEnabled = enabled;
        emit AutomaticSet(enabled);
    }

    function setTransferStepGasLimit(uint32 gasLimit) external onlyOwner whenIdle {
        if (gasLimit < 100_000 || gasLimit > 10_000_000) revert BadConfiguration();
        transferStepGasLimit = gasLimit;
        emit TransferStepGasLimitSet(gasLimit);
    }

    function setHarvestGasPriceCeilingWei(uint256 gasPriceWei) external onlyOwner whenIdle {
        if (gasPriceWei == 0) revert BadConfiguration();
        harvestGasPriceCeilingWei = gasPriceWei;
        emit HarvestGasPriceCeilingSet(gasPriceWei);
    }

    function pendingReportCount() external view returns (uint256) {
        return _pendingReportTail - _pendingReportHead;
    }

    function pendingEntryCount() external view returns (uint256) {
        return _entries.length + _entryRetries.length;
    }

    function pendingExitCount() external view returns (uint256) {
        return _exits.length;
    }

    /// @notice Includes stale entries until a later processing call removes them.
    function pendingHarvestCount() external view returns (uint256) {
        return _harvests.length + _harvestRetries.length;
    }

    function nextAction() external view returns (address token, Step step, uint64 eligibleAt) {
        SelectedAction memory action = _selectNext();
        if (action.token != address(0)) {
            if (action.step == Step.Harvest) {
                return (action.token, action.step, launches[action.token].nextHarvestAt);
            }
            return (action.token, action.step, launches[action.token].nextActionAt);
        }
        if (_exits.length != 0) {
            token = _exits[0];
            return (token, Step.Exit, launches[token].nextActionAt);
        }
        if (_entryRetries.length != 0) {
            token = _entryRetries[0];
            return (token, Step.Open, launches[token].nextActionAt);
        }
        if (_harvestRetries.length != 0) {
            token = _harvestRetries[0];
            return (token, Step.Harvest, launches[token].nextHarvestAt);
        }
    }

    function _selectNext() private view returns (SelectedAction memory action) {
        if (_exits.length != 0 && launches[_exits[0]].nextActionAt <= block.timestamp) {
            return SelectedAction(_exits[0], Step.Exit, 2);
        }
        if (_opensSinceHarvest >= 4 && tx.gasprice <= harvestGasPriceCeilingWei) {
            action = _selectHarvest();
            if (action.token != address(0)) return action;
        }
        if (_entries.length != 0) {
            return SelectedAction(_entries[_entries.length - 1], Step.Open, 0);
        }
        if (_entryRetries.length != 0 && launches[_entryRetries[0]].nextActionAt <= block.timestamp) {
            return SelectedAction(_entryRetries[0], Step.Open, 1);
        }
        return _selectHarvest();
    }

    function _selectHarvest() private view returns (SelectedAction memory action) {
        if (_harvests.length != 0) {
            return SelectedAction(_harvests[_harvests.length - 1], Step.Harvest, 3);
        }
        if (_harvestRetries.length != 0 && launches[_harvestRetries[0]].nextHarvestAt <= block.timestamp) {
            return SelectedAction(_harvestRetries[0], Step.Harvest, 4);
        }
    }

    function _update(address from, address to, uint256 amount) internal override {
        super._update(from, to, amount);
        if (
            automaticEnabled && !_processing && amount != 0 && from != address(0) &&
            to != address(0) && msg.sender == from && msg.sender.code.length == 0 &&
            !internalEndpoint[msg.sender] &&
            !internalEndpoint[from] && !internalEndpoint[to]
        ) {
            uint256 stepGas = uint256(transferStepGasLimit) + POST_CALL_GAS_RESERVE +
                TRANSFER_STEP_OVERHEAD;
            if (gasleft() > stepGas + OUTER_TRANSFER_GAS_RESERVE) {
                try this.processTransferStep{gas: stepGas}() returns (bool, bool) {} catch {}
            }
        }
    }

    /// @dev At most one state-changing executor call. Failure is caught and
    /// moved to timed retry, preserving ordinary transfer behavior. Both
    /// entry points use the same fixed executor gas cap, so a caller cannot
    /// force an underfunded attempt and thereby postpone an exit.
    function _processNext() private returns (bool attempted, bool succeeded) {
        bool reportApplied = _applyPendingReport();
        SelectedAction memory action = _selectNext();
        if (action.token == address(0)) return (reportApplied, reportApplied);
        return _executeNext(action, reportApplied);
    }

    function _executeNext(SelectedAction memory action, bool reportApplied)
        private returns (bool attempted, bool succeeded)
    {
        address token = action.token;
        Step step = action.step;
        uint8 source = action.source;

        if (step == Step.Open && launches[token].stage == Stage.Skipped) {
            if (source == 1) _heapPop(_entryRetries, false);
            else _entries.pop();
            emit SkippedEntryRemoved(token);
            return (true, true);
        }
        if (step == Step.Exit && launches[token].stage != Stage.Active) {
            _heapPop(_exits, false);
            emit StaleExitRemoved(token);
            return (true, true);
        }
        if (
            step == Step.Harvest &&
            (launches[token].stage != Stage.Active || !launches[token].harvestReady)
        ) {
            if (source == 3) _harvests.pop();
            else _heapPop(_harvestRetries, true);
            emit StaleHarvestRemoved(token);
            return (true, true);
        }

        uint256 gasLimit = transferStepGasLimit;
        uint256 previewBudget = step == Step.Harvest ? HARVEST_PREVIEW_GAS_LIMIT + 40_000 : 0;
        if (gasleft() <= gasLimit + POST_CALL_GAS_RESERVE + previewBudget) {
            return (reportApplied, reportApplied);
        }
        // A caller may pay any priority fee. It must not be able to move the
        // global harvest retry time by choosing an artificially high gas price.
        if (step == Step.Harvest && tx.gasprice > harvestGasPriceCeilingWei) {
            return (reportApplied, reportApplied);
        }

        attempted = true;
        _processing = true;
        _processingToken = token;
        _processingStep = step;
        if (step == Step.Harvest) {
            uint256 grossEthValue;
            uint256 estimatedGasUnits;
            try executor.previewHarvest{gas: HARVEST_PREVIEW_GAS_LIMIT}(token) returns (
                uint256 grossEthValue_, uint256 estimatedGasUnits_
            ) {
                grossEthValue = grossEthValue_;
                estimatedGasUnits = estimatedGasUnits_;
            } catch {
                _opensSinceHarvest = 0;
                _onFailure(token, step, source);
                _clearProcessing();
                return (true, false);
            }
            if (!_harvestWorthGas(grossEthValue, estimatedGasUnits)) {
                _opensSinceHarvest = 0;
                _onHarvestDeferred(token, source, grossEthValue, estimatedGasUnits);
                _clearProcessing();
                return (true, false);
            }
            if (gasleft() <= gasLimit + POST_CALL_GAS_RESERVE) {
                _clearProcessing();
                return (reportApplied, reportApplied);
            }
        }
        if (step == Step.Harvest) _opensSinceHarvest = 0;
        else if (step == Step.Open && _opensSinceHarvest < 4) ++_opensSinceHarvest;
        bool executed;
        int32 netReturnBps;
        bool comparable;
        if (address(executor).code.length != 0) {
            if (step == Step.Open) {
                // Reserve once, before the executor call. If open reverts,
                // this policy assignment survives the caught failure.
                uint24 feePips = feePolicy.selectFee(token);
                try this.executeOpenWithMint{gas: gasLimit}(token, feePips) { executed = true; } catch {}
            } else if (step == Step.Exit) {
                try executor.exit{gas: gasLimit}(token) returns (int32 observedBps, bool comparable_) {
                    netReturnBps = observedBps;
                    comparable = comparable_;
                    executed = true;
                } catch {}
            } else {
                try executor.harvest{gas: gasLimit}(token) { executed = true; } catch {}
            }
        }
        if (executed) {
            bool finalExit;
            if (step == Step.Exit) {
                uint256 remaining = executor.activePositionCount(token);
                if (remaining > 3) revert BadConfiguration();
                finalExit = remaining == 0;
                if (finalExit) {
                    // A failed policy write reverts this scheduler subcall and
                    // therefore the final executor exit.
                    if (comparable) {
                        if (netReturnBps > 10_000) netReturnBps = 10_000;
                        if (netReturnBps < -10_000) netReturnBps = -10_000;
                        feePolicy.recordClosed(token, netReturnBps);
                    } else {
                        uint64 deadline = uint64(block.timestamp) + OUTCOME_REPORT_WINDOW;
                        outcomeDeadline[token] = deadline;
                        emit FeeOutcomePending(token, deadline);
                    }
                }
            }
            _onSuccess(token, step, source, finalExit);
            succeeded = true;
            emit StepSucceeded(token, step);
            if (finalExit) emit AllPositionsExited(token);
        } else {
            _onFailure(token, step, source);
        }
        _clearProcessing();
    }

    function _reportable(Launch storage launch, address token) private view returns (bool) {
        return launch.stage == Stage.Active ||
            (_processing && launch.stage == Stage.Queued &&
                _processingToken == token && _processingStep == Step.Open);
    }

    function _queuePendingReport(address token, PendingReport storage pending) private {
        if (pending.queued) return;
        pending.queued = true;
        _pendingReports[_pendingReportTail++] = token;
        emit ReportDeferred(token);
    }

    function _applyPendingReport() private returns (bool consumed) {
        if (_pendingReportHead == _pendingReportTail) return false;
        address token = _pendingReports[_pendingReportHead];
        delete _pendingReports[_pendingReportHead++];
        Launch storage launch = launches[token];
        PendingReport memory pending = _pendingByToken[token];
        delete _pendingByToken[token];
        if (launch.stage != Stage.Active) return true;
        if (pending.bandAt != 0 && launch.enteredBandAt == 0) {
            launch.enteredBandAt = pending.bandAt;
            emit BandEntered(token);
        }
        if (pending.exit && launch.enteredBandAt != 0 && !launch.exitReady) {
            launch.exitReady = true;
            launch.quoteBoundary = pending.quoteBoundary;
            launch.nextActionAt = uint64(block.timestamp);
            _heapPush(_exits, token, false);
            emit ExitReady(token, pending.quoteBoundary);
        }
        if (pending.harvest && !launch.harvestReady) {
            launch.harvestReady = true;
            _harvests.push(token);
            emit HarvestReady(token);
        }
        return true;
    }

    function _clearProcessing() private {
        _processing = false;
        _processingToken = address(0);
        delete _processingStep;
    }

    function _harvestWorthGas(uint256 grossEthValue, uint256 estimatedGasUnits) private view returns (bool) {
        if (grossEthValue == 0 || estimatedGasUnits == 0) return false;
        uint256 price = harvestGasPriceCeilingWei;
        if (estimatedGasUnits > type(uint256).max / price) return false;
        uint256 gasCost = estimatedGasUnits * price;
        return gasCost <= type(uint256).max / HARVEST_GAS_VALUE_MULTIPLIER &&
            grossEthValue >= gasCost * HARVEST_GAS_VALUE_MULTIPLIER;
    }

    function _onHarvestDeferred(
        address token, uint8 source, uint256 grossEthValue, uint256 estimatedGasUnits
    ) private {
        Launch storage launch = launches[token];
        launch.nextHarvestAt = uint64(block.timestamp) + retryDelaySeconds;
        if (source == 3) {
            _harvests.pop();
            _heapPush(_harvestRetries, token, true);
        } else {
            _heapDown(_harvestRetries, 0, true);
        }
        emit HarvestDeferred(token, grossEthValue, estimatedGasUnits, launch.nextHarvestAt);
    }

    function _onSuccess(address token, Step step, uint8 source, bool finalExit) private {
        Launch storage launch = launches[token];
        if (step == Step.Harvest) {
            if (source == 3) _harvests.pop();
            else _heapPop(_harvestRetries, true);
            launch.harvestReady = false;
            launch.harvestFailures = 0;
            launch.nextHarvestAt = 0;
        } else if (step == Step.Open) {
            if (source == 1) _heapPop(_entryRetries, false);
            else _entries.pop();
            launch.stage = Stage.Active;
            if (block.timestamp > type(uint64).max) revert BadConfiguration();
            launch.activeAt = uint64(block.timestamp);
        } else {
            _heapPop(_exits, false);
            launch.stage = finalExit ? Stage.Exited : Stage.Active;
            launch.exitReady = false;
            launch.quoteBoundary = false;
            launch.harvestReady = false;
            if (!finalExit && block.timestamp >= uint256(launch.activeAt) + WIND_DOWN_DELAY) {
                if (block.timestamp > type(uint64).max) revert BadConfiguration();
                launch.exitReady = true;
                launch.nextActionAt = uint64(block.timestamp);
                _heapPush(_exits, token, false);
                emit WindDownRequested(token);
            }
        }
        if (step != Step.Harvest) {
            launch.failures = 0;
            if (step != Step.Exit || finalExit || !launch.exitReady) launch.nextActionAt = 0;
        }
    }

    function _onFailure(address token, Step step, uint8 source) private {
        Launch storage launch = launches[token];
        if (step == Step.Harvest) {
            if (launch.harvestFailures != type(uint32).max) ++launch.harvestFailures;
            launch.nextHarvestAt = uint64(block.timestamp) + retryDelaySeconds;
            if (source == 3) {
                _harvests.pop();
                _heapPush(_harvestRetries, token, true);
            } else {
                _heapDown(_harvestRetries, 0, true);
            }
            emit StepFailed(token, step, launch.harvestFailures, launch.nextHarvestAt);
            return;
        }
        if (launch.failures != type(uint32).max) ++launch.failures;
        if (step != Step.Exit) {
            if (source == 1) _heapPop(_entryRetries, false);
            else _entries.pop();
        }
        launch.nextActionAt = uint64(block.timestamp) + retryDelaySeconds;
        if (step == Step.Exit) _heapDown(_exits, 0, false);
        else _heapPush(_entryRetries, token, false);
        emit StepFailed(token, step, launch.failures, launch.nextActionAt);
    }

    function _less(address a, address b, bool harvestHeap) private view returns (bool) {
        uint64 aTime = harvestHeap ? launches[a].nextHarvestAt : launches[a].nextActionAt;
        uint64 bTime = harvestHeap ? launches[b].nextHarvestAt : launches[b].nextActionAt;
        return aTime < bTime || (aTime == bTime && a < b);
    }

    function _heapPush(address[] storage heap, address token, bool harvestHeap) private {
        heap.push(token);
        uint256 i = heap.length - 1;
        while (i != 0) {
            uint256 parent = (i - 1) / 2;
            if (!_less(token, heap[parent], harvestHeap)) break;
            heap[i] = heap[parent];
            i = parent;
        }
        heap[i] = token;
    }

    function _heapPop(address[] storage heap, bool harvestHeap) private {
        uint256 last = heap.length - 1;
        address tail = heap[last];
        heap.pop();
        if (last != 0) {
            heap[0] = tail;
            _heapDown(heap, 0, harvestHeap);
        }
    }

    function _heapDown(address[] storage heap, uint256 i, bool harvestHeap) private {
        uint256 n = heap.length;
        address item = heap[i];
        while (true) {
            uint256 left = 2 * i + 1;
            if (left >= n) break;
            uint256 right = left + 1;
            uint256 child = right < n && _less(heap[right], heap[left], harvestHeap) ? right : left;
            if (!_less(heap[child], item, harvestHeap)) break;
            heap[i] = heap[child];
            i = child;
        }
        heap[i] = item;
    }
}
