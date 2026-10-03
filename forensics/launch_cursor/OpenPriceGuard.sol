// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

/// @notice Only the fields needed to authenticate an active, ETH-paired Pons V2 launch.
interface IOpenPricePonsFactory {
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
    function memeHook() external view returns (address);
    function poolManager() external view returns (address);
}

interface IOpenPricePonsCurve {
    function token() external view returns (address);
    function pairToken() external view returns (address);
    function factory() external view returns (address);
    function graduated() external view returns (bool);
    function readyToGraduate() external view returns (bool);
    function getReserves() external view returns (uint256 quoteReserve, uint256 tokenReserve);
}

interface IOpenPriceStateView {
    function poolManager() external view returns (address);
    function getSlot0(bytes32 poolId)
        external
        view
        returns (uint160 sqrtPriceX96, int24 tick, uint24 protocolFee, uint24 lpFee);
    function getLiquidity(bytes32 poolId) external view returns (uint128);
}

interface IOpenPricePonsHook {
    function factory() external view returns (address);
    function poolManager() external view returns (address);
}

/// @dev 512-bit multiply/divide from Uniswap v4 FullMath (MIT), kept here so
/// this read-only guard has no package imports. Reverts if the quotient is too
/// large for uint256 or the denominator is zero.
library OpenPriceFullMath {
    function mulDiv(uint256 a, uint256 b, uint256 denominator) internal pure returns (uint256 result) {
        unchecked {
            uint256 prod0 = a * b;
            uint256 prod1;
            assembly ("memory-safe") {
                let mm := mulmod(a, b, not(0))
                prod1 := sub(sub(mm, prod0), lt(mm, prod0))
            }
            require(denominator > prod1, "mulDiv overflow");
            if (prod1 == 0) return prod0 / denominator;

            uint256 remainder;
            assembly ("memory-safe") {
                remainder := mulmod(a, b, denominator)
                prod1 := sub(prod1, gt(remainder, prod0))
                prod0 := sub(prod0, remainder)
            }
            uint256 twos = (0 - denominator) & denominator;
            assembly ("memory-safe") {
                denominator := div(denominator, twos)
                prod0 := div(prod0, twos)
                twos := add(div(sub(0, twos), twos), 1)
            }
            prod0 |= prod1 * twos;
            uint256 inverse = (3 * denominator) ^ 2;
            inverse *= 2 - denominator * inverse;
            inverse *= 2 - denominator * inverse;
            inverse *= 2 - denominator * inverse;
            inverse *= 2 - denominator * inverse;
            inverse *= 2 - denominator * inverse;
            inverse *= 2 - denominator * inverse;
            result = prod0 * inverse;
        }
    }
}

/// @notice Checks a proposed hookless X/Q starting price against Pons X/ETH
/// curve reserves (phase 0) or its graduated v4 pool (phase 2), composed with
/// the configured Q/ETH v4 pool. Prices use raw currency units.
/// @dev This is a *spot sanity bound*, not an oracle or an executable quote.
/// Both venues can move or be manipulated before the check. The guard does
/// not measure Q/ETH depth, v4 fees/impact, Pons fees/tax, or future value.
/// A phase-0 curve already ready to graduate is rejected because its trades
/// have closed; phase 1 (swept) has no tradable Pons price here.
contract OpenPriceGuard {
    uint256 private constant Q96 = 1 << 96;
    uint256 private constant Q128 = 1 << 128;
    uint256 private constant Q192 = 1 << 192;
    uint256 private constant BPS = 10_000;
    uint160 private constant MIN_SQRT_PRICE = 4_295_128_739;
    uint160 private constant MAX_SQRT_PRICE = 1_461_446_703_485_210_103_287_273_052_203_988_822_378_723_970_342;

    IOpenPricePonsFactory public immutable ponsFactory;
    IOpenPriceStateView public immutable stateView;
    address public immutable ponsHook;
    address public immutable quoteToken;
    bytes32 public immutable quoteEthPoolId;
    uint24 public immutable quoteEthFeePips;
    int24 public immutable quoteEthTickSpacing;
    uint16 public immutable maxDeviationBps;

    error BadConfiguration();
    error NotEthPonsToken();
    error UnsupportedLaunchPhase(uint8 phase);
    error QuotePoolUnavailable();
    error GraduatedPoolUnavailable();
    error CrossPriceOutOfRange();
    error PriceOutsideBound(uint160 proposedSqrtPriceX96, uint160 referenceSqrtPriceX96);

    /// @param quoteEthFeePips_ Fee of Q's *actual* launch pool (currently 2,500 pips).
    /// @param quoteEthTickSpacing_ Spacing of that pool (current Instant Launch: 25;
    /// earlier deployments used 60). This is unrelated to the X/Q pool spacing.
    /// @param maxDeviationBps_ Maximum price deviation; capped at 1,000 bps.
    constructor(
        address ponsFactory_,
        address stateView_,
        address quoteToken_,
        uint24 quoteEthFeePips_,
        int24 quoteEthTickSpacing_,
        uint16 maxDeviationBps_
    ) {
        if (
            ponsFactory_.code.length == 0 || stateView_.code.length == 0 || quoteToken_.code.length == 0 ||
            quoteEthFeePips_ > 1_000_000 || quoteEthTickSpacing_ < 1 ||
            quoteEthTickSpacing_ > type(int16).max ||
            maxDeviationBps_ == 0 || maxDeviationBps_ > 1_000
        ) revert BadConfiguration();
        address manager = IOpenPriceStateView(stateView_).poolManager();
        address hook = IOpenPricePonsFactory(ponsFactory_).memeHook();
        if (
            manager.code.length == 0 || hook.code.length == 0 ||
            IOpenPricePonsFactory(ponsFactory_).poolManager() != manager ||
            IOpenPricePonsHook(hook).factory() != ponsFactory_ ||
            IOpenPricePonsHook(hook).poolManager() != manager
        ) revert BadConfiguration();

        ponsFactory = IOpenPricePonsFactory(ponsFactory_);
        stateView = IOpenPriceStateView(stateView_);
        ponsHook = hook;
        quoteToken = quoteToken_;
        quoteEthFeePips = quoteEthFeePips_;
        quoteEthTickSpacing = quoteEthTickSpacing_;
        maxDeviationBps = maxDeviationBps_;
        // v4 PoolId = keccak256(abi.encode(PoolKey)). Native ETH sorts first.
        quoteEthPoolId = keccak256(
            abi.encode(address(0), quoteToken_, quoteEthFeePips_, quoteEthTickSpacing_, address(0))
        );
    }

    /// @notice Marginal X/Q sqrt price implied by the ETH-paired Pons phase-0
    /// curve or phase-2 pool and Q/ETH pool. Phase 1 and phase 3 revert.
    function referenceSqrtPriceX96(address launchToken) public view returns (uint160 refSqrt) {
        IOpenPricePonsFactory.LaunchedToken memory launch = ponsFactory.getLaunchedToken(launchToken);
        if (
            launchToken == address(0) || launchToken == quoteToken || !launch.exists ||
            launch.token != launchToken || launch.curve.code.length == 0 ||
            launch.pairToken != address(0)
        ) revert NotEthPonsToken();
        if (launch.phase != 0 && launch.phase != 2) revert UnsupportedLaunchPhase(launch.phase);

        (uint160 quoteEthSqrt,,, uint24 liveFee) = stateView.getSlot0(quoteEthPoolId);
        if (
            quoteEthSqrt < MIN_SQRT_PRICE || quoteEthSqrt >= MAX_SQRT_PRICE ||
            liveFee != quoteEthFeePips || stateView.getLiquidity(quoteEthPoolId) == 0
        ) revert QuotePoolUnavailable();

        uint256 quotePerTokenSqrt = launch.phase == 0
            ? _curveQuotePerTokenSqrt(launchToken, launch.curve, quoteEthSqrt)
            : _graduatedQuotePerTokenSqrt(launchToken, launch, quoteEthSqrt);
        if (quotePerTokenSqrt == 0) revert CrossPriceOutOfRange();
        uint256 oriented = launchToken < quoteToken
            ? quotePerTokenSqrt  // (currency0=X, currency1=Q): sqrt(Q/X)
            : Q192 / quotePerTokenSqrt; // (currency0=Q, currency1=X): sqrt(X/Q)
        if (oriented < MIN_SQRT_PRICE || oriented >= MAX_SQRT_PRICE) revert CrossPriceOutOfRange();
        refSqrt = uint160(oriented);
    }

    /// @dev Once readyToGraduate() is true, its reserve ratio is no longer a
    /// tradable price; wait for a verified graduated pool instead.
    function _curveQuotePerTokenSqrt(address launchToken, address curveAddress, uint160 quoteEthSqrt)
        private view returns (uint256)
    {
        IOpenPricePonsCurve curve = IOpenPricePonsCurve(curveAddress);
        if (
            curve.factory() != address(ponsFactory) || curve.token() != launchToken ||
            curve.pairToken() != address(0) || curve.graduated() || curve.readyToGraduate()
        ) revert NotEthPonsToken();
        (uint256 ethReserve, uint256 tokenReserve) = curve.getReserves();
        if (ethReserve == 0 || tokenReserve == 0) revert CrossPriceOutOfRange();

        // sqrt(ETH raw / X raw), scaled by Q96. The 2^64 guard keeps the
        // 512-bit quotient representable; the lower bound makes rounding
        // error in the subsequent product less than 2^-64 relatively.
        if (ethReserve / tokenReserve >= (1 << 64)) revert CrossPriceOutOfRange();
        uint256 reserveRatioQ192 = OpenPriceFullMath.mulDiv(ethReserve, Q192, tokenReserve);
        uint256 sqrtReserveRatioX96 = _sqrt(reserveRatioQ192);
        if (sqrtReserveRatioX96 < (1 << 64)) revert CrossPriceOutOfRange();
        return OpenPriceFullMath.mulDiv(uint256(quoteEthSqrt), sqrtReserveRatioX96, Q96);
    }

    /// @dev Pons graduates native pairs into (ETH=currency0, X=currency1,
    /// recorded poolFee, recorded tickSpacing, shared memeHook). Thus its
    /// sqrt price is sqrt(X/ETH); divide Q/ETH sqrt by it to get sqrt(Q/X).
    function _graduatedQuotePerTokenSqrt(
        address launchToken,
        IOpenPricePonsFactory.LaunchedToken memory launch,
        uint160 quoteEthSqrt
    ) private view returns (uint256) {
        if (
            launch.poolFee > 1_000_000 || launch.tickSpacing < 1 ||
            launch.tickSpacing > type(int16).max
        ) revert GraduatedPoolUnavailable();
        bytes32 poolId = keccak256(
            abi.encode(address(0), launchToken, launch.poolFee, launch.tickSpacing, ponsHook)
        );
        (uint160 ponsSqrt,,, uint24 liveFee) = stateView.getSlot0(poolId);
        if (
            ponsSqrt < MIN_SQRT_PRICE || ponsSqrt >= MAX_SQRT_PRICE ||
            liveFee != launch.poolFee || stateView.getLiquidity(poolId) == 0
        ) revert GraduatedPoolUnavailable();
        return OpenPriceFullMath.mulDiv(uint256(quoteEthSqrt), Q96, uint256(ponsSqrt));
    }

    /// @notice Returns false for a proposed price outside the configured
    /// *price* deviation, and reverts if the live sources are invalid.
    function isWithinBound(address launchToken, uint160 proposedSqrtPriceX96) external view returns (bool) {
        return _withinBound(proposedSqrtPriceX96, referenceSqrtPriceX96(launchToken));
    }

    /// @notice Reverts on deviation and otherwise returns the live reference.
    /// Call during the same transaction as X/Q pool initialization and mint.
    function validate(address launchToken, uint160 proposedSqrtPriceX96)
        external
        view
        returns (uint160 refSqrt)
    {
        refSqrt = referenceSqrtPriceX96(launchToken);
        if (!_withinBound(proposedSqrtPriceX96, refSqrt)) {
            revert PriceOutsideBound(proposedSqrtPriceX96, refSqrt);
        }
    }

    function _withinBound(uint160 proposed, uint160 refSqrt) private view returns (bool) {
        if (proposed < MIN_SQRT_PRICE || proposed >= MAX_SQRT_PRICE) return false;
        // At <=10% allowed price deviation, sqrt(price) is always <2x.
        if (uint256(proposed) > uint256(refSqrt) * 2) return false;
        uint256 sqrtRatioQ128 = OpenPriceFullMath.mulDiv(uint256(proposed), Q128, uint256(refSqrt));
        uint256 priceRatioQ128 = OpenPriceFullMath.mulDiv(sqrtRatioQ128, sqrtRatioQ128, Q128);
        uint256 lower = OpenPriceFullMath.mulDiv(Q128, BPS - maxDeviationBps, BPS);
        uint256 upper = OpenPriceFullMath.mulDiv(Q128, BPS + maxDeviationBps, BPS);
        return priceRatioQ128 >= lower && priceRatioQ128 <= upper;
    }

    function _sqrt(uint256 a) private pure returns (uint256 result) {
        if (a == 0) return 0;
        uint256 x = a;
        uint256 msb;
        if (x >= 1 << 128) { x >>= 128; msb += 128; }
        if (x >= 1 << 64) { x >>= 64; msb += 64; }
        if (x >= 1 << 32) { x >>= 32; msb += 32; }
        if (x >= 1 << 16) { x >>= 16; msb += 16; }
        if (x >= 1 << 8) { x >>= 8; msb += 8; }
        if (x >= 1 << 4) { x >>= 4; msb += 4; }
        if (x >= 1 << 2) { x >>= 2; msb += 2; }
        if (x >= 1 << 1) msb += 1;
        result = 1 << (msb >> 1);
        unchecked {
            for (uint256 i; i < 7; ++i) result = (result + a / result) >> 1;
        }
        uint256 other = a / result;
        return result < other ? result : other;
    }
}
