// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

/// @notice Pons V2's exact launch-record ABI. Native ETH is pairToken zero;
/// phase zero is the active curve, while phase two trades on Uniswap v4.
interface IPonsActiveExitFactory {
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

interface IPonsActiveExitCurve {
    function factory() external view returns (address);
    function token() external view returns (address);
    function pairToken() external view returns (address);
    function isNativeQuote() external view returns (bool);
    function graduated() external view returns (bool);
    function readyToGraduate() external view returns (bool);
    function getReserves() external view returns (uint256 quoteReserve, uint256 tokenReserve);
    function feeBps() external view returns (uint256);
    function creatorTaxBps() external view returns (uint256);
    function sell(uint256 tokensIn, uint256 minQuoteOut, address recipient) external returns (uint256 quoteOut);
}

interface IPonsActiveExitToken {
    function balanceOf(address account) external view returns (uint256);
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
    function approve(address spender, uint256 amount) external returns (bool);
}

/// @notice A narrow, standalone adapter for selling X into an active,
/// native-ETH Pons V2 curve. The authorized source must own X and approve this
/// adapter first. A later executor integration must supply that call path.
///
/// This is not a complete X/Q exit: it cannot withdraw the executor's LP,
/// buy or burn Q, route a graduated launch, or split ETH/WETH payouts.
contract PonsActiveExitAdapter {
    IPonsActiveExitFactory public immutable factory;
    address public immutable source;

    uint256 private _entered = 1;
    address private _expectedCurve;

    event ActiveCurveSold(
        address indexed token, address indexed curve, address indexed recipient, uint256 tokensIn, uint256 ethOut
    );

    error InvalidConfiguration();
    error Unauthorized();
    error ReentrantCall();
    error Expired();
    error InactiveOrNonNativeLaunch();
    error InvalidAmount();
    error TokenCallFailed();
    error TokenAmountMismatch();
    error NativeAmountMismatch();
    error NativeTransferFailed();
    error UnexpectedNativeSender();

    modifier onlySource() {
        if (msg.sender != source) revert Unauthorized();
        _;
    }

    modifier nonReentrant() {
        if (_entered != 1) revert ReentrantCall();
        _entered = 2;
        _;
        _entered = 1;
    }

    constructor(address factory_, address source_) {
        if (factory_.code.length == 0 || source_ == address(0)) revert InvalidConfiguration();
        factory = IPonsActiveExitFactory(factory_);
        source = source_;
    }

    /// @notice Preview Pons's current integer-exact sell result, including its
    /// base fee and creator tax. State can move before inclusion; callers must
    /// independently choose a positive `minEthOut` for the actual sale.
    function previewNativeActiveSale(address token, uint256 tokensIn)
        external
        view
        returns (address curve, uint256 ethOut)
    {
        if (tokensIn == 0) revert InvalidAmount();
        IPonsActiveExitCurve active = _activeCurve(token);
        (uint256 quoteReserve, uint256 tokenReserve) = active.getReserves();
        uint256 fee = active.feeBps();
        uint256 tax = active.creatorTaxBps();
        if (quoteReserve == 0 || tokenReserve == 0 || fee + tax >= 10_000) revert InvalidConfiguration();

        // Match PonsV2BondingCurveMath.getAmountOut(..., feeBps=0), followed
        // by the two independent output-side fee floors in curve.sell().
        uint256 inputScaled = tokensIn * 10_000;
        uint256 gross = inputScaled * quoteReserve / (tokenReserve * 10_000 + inputScaled);
        if (gross == 0) revert InvalidAmount();
        ethOut = gross - (gross * fee / 10_000) - (gross * tax / 10_000);
        curve = address(active);
    }

    /// @notice Pull exact X from `source`, sell it on its registered active
    /// native curve, then forward only this sale's ETH to `recipient`.
    /// Every step reverts together if phase, allowance, output, or payout fails.
    function sellNativeActiveCurve(
        address token,
        uint256 tokensIn,
        uint256 minEthOut,
        address payable recipient,
        uint64 deadline
    ) external onlySource nonReentrant returns (uint256 ethOut) {
        if (block.timestamp > deadline) revert Expired();
        if (tokensIn == 0 || minEthOut == 0 || recipient == address(0)) revert InvalidAmount();

        IPonsActiveExitCurve active = _activeCurve(token);
        IPonsActiveExitToken x = IPonsActiveExitToken(token);
        uint256 tokenBefore = x.balanceOf(address(this));
        _safeTransferFrom(token, source, address(this), tokensIn);
        if (x.balanceOf(address(this)) - tokenBefore != tokensIn) revert TokenAmountMismatch();

        uint256 ethBefore = address(this).balance;
        _safeApprove(token, address(active), 0);
        _safeApprove(token, address(active), tokensIn);
        _expectedCurve = address(active);
        uint256 reported = active.sell(tokensIn, minEthOut, address(this));
        _expectedCurve = address(0);
        _safeApprove(token, address(active), 0);

        ethOut = address(this).balance - ethBefore;
        if (ethOut != reported || ethOut < minEthOut) revert NativeAmountMismatch();
        if (x.balanceOf(address(this)) != tokenBefore) revert TokenAmountMismatch();
        (bool sent,) = recipient.call{value: ethOut}("");
        if (!sent) revert NativeTransferFailed();
        emit ActiveCurveSold(token, address(active), recipient, tokensIn, ethOut);
    }

    receive() external payable {
        if (msg.sender != _expectedCurve) revert UnexpectedNativeSender();
    }

    function _activeCurve(address token) private view returns (IPonsActiveExitCurve active) {
        if (token == address(0) || token.code.length == 0) revert InactiveOrNonNativeLaunch();
        IPonsActiveExitFactory.LaunchedToken memory launch = factory.getLaunchedToken(token);
        if (
            !launch.exists || launch.token != token || launch.curve.code.length == 0 || launch.pairToken != address(0)
                || launch.phase != 0
        ) revert InactiveOrNonNativeLaunch();

        active = IPonsActiveExitCurve(launch.curve);
        if (
            active.factory() != address(factory) || active.token() != token || active.pairToken() != address(0)
                || !active.isNativeQuote() || active.graduated() || active.readyToGraduate()
        ) revert InactiveOrNonNativeLaunch();
    }

    function _safeTransferFrom(address token, address from, address to, uint256 amount) private {
        (bool ok, bytes memory result) =
            token.call(abi.encodeWithSelector(IPonsActiveExitToken.transferFrom.selector, from, to, amount));
        if (!ok || (result.length != 0 && (result.length != 32 || !abi.decode(result, (bool))))) {
            revert TokenCallFailed();
        }
    }

    function _safeApprove(address token, address spender, uint256 amount) private {
        (bool ok, bytes memory result) =
            token.call(abi.encodeWithSelector(IPonsActiveExitToken.approve.selector, spender, amount));
        if (!ok || (result.length != 0 && (result.length != 32 || !abi.decode(result, (bool))))) {
            revert TokenCallFailed();
        }
    }
}
