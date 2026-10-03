// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {PonsActiveExitAdapter, IPonsActiveExitFactory} from "./PonsActiveExitAdapter.sol";
import {PonsGraduatedSwapAdapter} from "./PonsGraduatedSwapAdapter.sol";
import {HooklessQuoteBuyAdapter} from "./HooklessQuoteBuyAdapter.sol";

interface IExitSettlementToken {
    function balanceOf(address account) external view returns (uint256);
    function totalSupply() external view returns (uint256);
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
    function transfer(address to, uint256 amount) external returns (bool);
    function approve(address spender, uint256 amount) external returns (bool);
    function burn(uint256 amount) external;
}

interface IExitSettlementWETH {
    function balanceOf(address account) external view returns (uint256);
    function deposit() external payable;
    function transfer(address to, uint256 amount) external returns (bool);
}

/// @notice Atomic settlement for a withdrawn X/Q position. The configured
/// executor supplies its exact X and Q receipts and approves this router.
/// @dev Sells all X on Pons, spends half of its ETH proceeds on Q, burns all
/// recovered/bought Q, then splits the remaining ETH into WETH for the wizard
/// fanout and native ETH for the developer. It does not withdraw LPs or value
/// gas and does not report strategy ROI.
contract ExitSettlementRouter {
    address private constant PONS_FACTORY = 0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e;
    address private constant V4_POOL_MANAGER = 0x8366a39CC670B4001A1121B8F6A443A643e40951;
    uint256 private constant MAX_DEADLINE_DELAY = 15 minutes;

    IPonsActiveExitFactory public immutable factory;
    IExitSettlementToken public immutable quoteToken;
    IExitSettlementWETH public immutable weth;
    address public immutable source;
    address public immutable wizardFanout;
    address payable public immutable developer;
    PonsActiveExitAdapter public immutable activeSale;
    PonsGraduatedSwapAdapter public immutable graduatedSale;
    HooklessQuoteBuyAdapter public immutable quoteBuy;

    uint256 private _entered = 1;
    address private _expectedNativeSender;
    uint256 private _receivedNative;

    struct StartingBalances {
        uint256 native;
        uint256 quote;
        uint256 wrapped;
        uint256 launchToken;
    }

    event ExitSettled(
        address indexed token,
        uint256 xSold,
        uint256 ethOut,
        uint256 quoteBought,
        uint256 quoteBurned,
        uint256 wizardWeth,
        uint256 developerEth
    );

    error InvalidConfiguration();
    error Unauthorized();
    error ReentrantCall();
    error Expired();
    error DeadlineTooFar();
    error InvalidAmount();
    error SweptPhase();
    error UnsupportedLaunch();
    error TokenCallFailed();
    error TokenAmountMismatch();
    error NativeAmountMismatch();
    error WrappedAmountMismatch();
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

    constructor(
        address source_,
        address quoteToken_,
        address weth_,
        address wizardFanout_,
        address payable developer_,
        uint24 quoteEthFee_,
        int24 quoteEthTickSpacing_
    ) {
        if (
            block.chainid != 4663 || source_ == address(0) || quoteToken_ == address(0) || quoteToken_.code.length == 0
                || weth_ == address(0) || weth_.code.length == 0 || wizardFanout_ == address(0)
                || wizardFanout_.code.length == 0 || developer_ == address(0) || PONS_FACTORY.code.length == 0
        ) revert InvalidConfiguration();

        source = source_;
        quoteToken = IExitSettlementToken(quoteToken_);
        weth = IExitSettlementWETH(weth_);
        wizardFanout = wizardFanout_;
        developer = developer_;
        factory = IPonsActiveExitFactory(PONS_FACTORY);

        // Child construction binds every adapter's source to this router.
        // The real factory, hook, and PoolManager are checked by the children.
        activeSale = new PonsActiveExitAdapter(PONS_FACTORY, address(this));
        graduatedSale = new PonsGraduatedSwapAdapter(PONS_FACTORY, address(this));
        quoteBuy = new HooklessQuoteBuyAdapter(
            V4_POOL_MANAGER, quoteToken_, quoteEthFee_, quoteEthTickSpacing_, address(this)
        );
    }

    /// @notice Settle exact receipts from `source`. The source must approve
    /// this router for X and Q before calling. If X is zero, only Q is burned;
    /// both minimums must be zero and no native or WETH payout is made.
    function settle(
        address token,
        uint256 xAmount,
        uint256 qAmount,
        uint256 minEthOut,
        uint256 minQOut,
        uint64 deadline
    ) external onlySource nonReentrant returns (uint256 ethOut, uint256 qBurned) {
        if (block.timestamp > deadline) revert Expired();
        if (deadline > block.timestamp + MAX_DEADLINE_DELAY) revert DeadlineTooFar();
        if (xAmount == 0) {
            if (qAmount == 0 || minEthOut != 0 || minQOut != 0) revert InvalidAmount();
        } else if (minEthOut == 0 || minQOut == 0 || token == address(0) || token == address(quoteToken)) {
            revert InvalidAmount();
        }

        StartingBalances memory start = StartingBalances({
            native: address(this).balance,
            quote: quoteToken.balanceOf(address(this)),
            wrapped: weth.balanceOf(address(this)),
            launchToken: 0
        });
        if (xAmount != 0) {
            _requireSalePhase(token);
            start.launchToken = IExitSettlementToken(token).balanceOf(address(this));
            _pullExact(token, xAmount);
        }
        if (qAmount != 0) _pullExact(address(quoteToken), qAmount);

        uint256 qBought;
        if (xAmount != 0) {
            ethOut = _sellAll(token, xAmount, minEthOut, deadline);
            if (IExitSettlementToken(token).balanceOf(address(this)) != start.launchToken) {
                revert TokenAmountMismatch();
            }
            qBought = _buyQuote(ethOut / 2, minQOut, deadline);
        }

        qBurned = qAmount + qBought;
        _burnQuote(qBurned, start.quote);

        uint256 toDistribute = ethOut - (ethOut / 2);
        uint256 wizardPayout = toDistribute / 2;
        uint256 devPayout = toDistribute - wizardPayout;
        _payWizard(wizardPayout, start.wrapped);
        _payDeveloper(devPayout);
        if (address(this).balance != start.native || weth.balanceOf(address(this)) != start.wrapped) {
            revert NativeAmountMismatch();
        }
        emit ExitSettled(token, xAmount, ethOut, qBought, qBurned, wizardPayout, devPayout);
    }

    receive() external payable {
        if (_entered != 2 || msg.sender != _expectedNativeSender || msg.value == 0 || _receivedNative != 0) {
            revert UnexpectedNativeSender();
        }
        _receivedNative = msg.value;
    }

    function _requireSalePhase(address token) private view {
        if (token.code.length == 0) revert UnsupportedLaunch();
        IPonsActiveExitFactory.LaunchedToken memory launch = factory.getLaunchedToken(token);
        if (!launch.exists || launch.token != token || launch.pairToken != address(0)) revert UnsupportedLaunch();
        if (launch.phase == 1) revert SweptPhase();
        if (launch.phase != 0 && launch.phase != 2) revert UnsupportedLaunch();
    }

    function _sellAll(address token, uint256 xAmount, uint256 minEthOut, uint64 deadline)
        private
        returns (uint256 ethOut)
    {
        uint8 phase = factory.getLaunchedToken(token).phase;
        address sale = phase == 0 ? address(activeSale) : address(graduatedSale);
        _safeApprove(token, sale, 0);
        _safeApprove(token, sale, xAmount);
        _expectedNativeSender = sale;
        uint256 nativeBefore = address(this).balance;
        if (phase == 0) {
            ethOut = activeSale.sellNativeActiveCurve(token, xAmount, minEthOut, payable(address(this)), deadline);
        } else if (phase == 2) {
            ethOut = graduatedSale.sellNativeGraduatedPool(token, xAmount, minEthOut, payable(address(this)), deadline);
        } else if (phase == 1) {
            revert SweptPhase();
        } else {
            revert UnsupportedLaunch();
        }
        _expectedNativeSender = address(0);
        _safeApprove(token, sale, 0);
        if (ethOut < minEthOut || ethOut != _receivedNative || address(this).balance - nativeBefore != ethOut) {
            revert NativeAmountMismatch();
        }
        _receivedNative = 0;
    }

    function _buyQuote(uint256 ethIn, uint256 minQOut, uint64 deadline) private returns (uint256 qBought) {
        if (ethIn == 0) revert InvalidAmount();
        uint256 quoteBefore = quoteToken.balanceOf(address(this));
        uint256 nativeBefore = address(this).balance;
        qBought = quoteBuy.buyQ{value: ethIn}(minQOut, address(this), deadline);
        if (
            address(this).balance != nativeBefore - ethIn
                || quoteToken.balanceOf(address(this)) - quoteBefore != qBought
        ) revert NativeAmountMismatch();
    }

    function _burnQuote(uint256 amount, uint256 quoteBefore) private {
        uint256 supplyBefore = quoteToken.totalSupply();
        quoteToken.burn(amount);
        if (quoteToken.balanceOf(address(this)) != quoteBefore || supplyBefore - quoteToken.totalSupply() != amount) {
            revert TokenAmountMismatch();
        }
    }

    function _payWizard(uint256 amount, uint256 wethBefore) private {
        if (amount == 0) return;
        uint256 nativeBefore = address(this).balance;
        weth.deposit{value: amount}();
        if (address(this).balance != nativeBefore - amount || weth.balanceOf(address(this)) - wethBefore != amount) {
            revert WrappedAmountMismatch();
        }
        uint256 fanoutBefore = weth.balanceOf(wizardFanout);
        _safeTransfer(address(weth), wizardFanout, amount);
        if (weth.balanceOf(address(this)) != wethBefore || weth.balanceOf(wizardFanout) - fanoutBefore != amount) {
            revert WrappedAmountMismatch();
        }
    }

    function _payDeveloper(uint256 amount) private {
        if (amount == 0) return;
        uint256 nativeBefore = address(this).balance;
        (bool sent,) = developer.call{value: amount}("");
        if (!sent) revert NativeTransferFailed();
        if (address(this).balance != nativeBefore - amount) revert NativeAmountMismatch();
    }

    function _pullExact(address token, uint256 amount) private {
        IExitSettlementToken asset = IExitSettlementToken(token);
        uint256 sourceBefore = asset.balanceOf(source);
        uint256 routerBefore = asset.balanceOf(address(this));
        _safeTransferFrom(token, source, address(this), amount);
        if (sourceBefore - asset.balanceOf(source) != amount || asset.balanceOf(address(this)) - routerBefore != amount)
        {
            revert TokenAmountMismatch();
        }
    }

    function _safeTransferFrom(address token, address from, address to, uint256 amount) private {
        (bool ok, bytes memory result) =
            token.call(abi.encodeWithSelector(IExitSettlementToken.transferFrom.selector, from, to, amount));
        if (!ok || (result.length != 0 && (result.length != 32 || !abi.decode(result, (bool))))) {
            revert TokenCallFailed();
        }
    }

    function _safeTransfer(address token, address to, uint256 amount) private {
        (bool ok, bytes memory result) =
            token.call(abi.encodeWithSelector(IExitSettlementWETH.transfer.selector, to, amount));
        if (!ok || (result.length != 0 && (result.length != 32 || !abi.decode(result, (bool))))) {
            revert TokenCallFailed();
        }
    }

    function _safeApprove(address token, address spender, uint256 amount) private {
        (bool ok, bytes memory result) =
            token.call(abi.encodeWithSelector(IExitSettlementToken.approve.selector, spender, amount));
        if (!ok || (result.length != 0 && (result.length != 32 || !abi.decode(result, (bool))))) {
            revert TokenCallFailed();
        }
    }
}
