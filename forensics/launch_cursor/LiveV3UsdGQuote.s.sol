// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

interface ILiveQuoteVm {
    function envOr(string calldata key, string calldata fallbackValue) external returns (string memory);
}

interface ILiveV4Quoter {
    struct PoolKey { address currency0; address currency1; uint24 fee; int24 tickSpacing; address hooks; }
    struct QuoteExactSingleParams { PoolKey poolKey; bool zeroForOne; uint128 exactAmount; bytes hookData; }
    function quoteExactInputSingle(QuoteExactSingleParams calldata params)
        external returns (uint256 amountOut, uint256 gasEstimate);
}

interface ILiveV3Quoter {
    struct QuoteExactInputSingleParams {
        address tokenIn; address tokenOut; uint256 amountIn; uint24 fee; uint160 sqrtPriceLimitX96;
    }
    function quoteExactInputSingle(QuoteExactInputSingleParams calldata params)
        external returns (uint256 amountOut, uint160 sqrtPriceX96After, uint32 initializedTicksCrossed, uint256 gasEstimate);
}

interface ILiveStateView {
    function getSlot0(bytes32 poolId) external view
        returns (uint160 sqrtPriceX96, int24 tick, uint24 protocolFee, uint24 lpFee);
    function getLiquidity(bytes32 poolId) external view returns (uint128 liquidity);
}

interface ILiveFactory { function getPool(address a, address b, uint24 fee) external view returns (address); }
interface ILiveToken { function balanceOf(address account) external view returns (uint256); }
interface ILiveQ {
    function canonicalV3Pool() external view returns (address);
    function owner() external view returns (address);
    function automaticEnabled() external view returns (bool);
}
interface ILiveV3Pool {
    function liquidity() external view returns (uint128);
    function slot0() external view returns (
        uint160 sqrtPriceX96, int24 tick, uint16 observationIndex, uint16 observationCardinality,
        uint16 observationCardinalityNext, uint8 feeProtocol, bool unlocked
    );
}

/// @notice Read-only fork quote probe. Invoke with forge script, never --broadcast.
contract LiveV3UsdGQuote {
    event log_named_uint(string key, uint256 value);
    event log_named_address(string key, address value);
    address constant Q = 0x623B5374c4CB838DA24EE9F48F08664337936a06;
    address constant OWNER = 0x26E8134eCC3af5cCE32f34B03E7BD2f318B25158;
    address constant USDG = 0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168;
    address constant WETH = 0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73;
    address constant V4_QUOTER = 0x8Dc178eFB8111BB0973Dd9d722ebeFF267c98F94;
    address constant V3_QUOTER = 0x33e885eD0Ec9bF04EcfB19341582aADCb4c8A9E7;
    address constant V3_FACTORY = 0x1f7d7550B1b028f7571E69A784071F0205FD2EfA;
    address constant V4_STATE_VIEW = 0xF3334192D15450CdD385c8B70e03f9A6bD9E673b;

    function status() external {
        address pool = ILiveFactory(V3_FACTORY).getPool(Q, USDG, 3000);
        emit log_named_uint("block", block.number);
        emit log_named_uint("timestamp", block.timestamp);
        emit log_named_uint("owner_eth", OWNER.balance);
        emit log_named_uint("owner_usdg", ILiveToken(USDG).balanceOf(OWNER));
        emit log_named_uint("owner_q", ILiveToken(Q).balanceOf(OWNER));
        emit log_named_address("q_owner", ILiveQ(Q).owner());
        emit log_named_address("bound_v3_pool", ILiveQ(Q).canonicalV3Pool());
        emit log_named_address("factory_v3_pool", pool);
        emit log_named_uint("automatic_enabled", ILiveQ(Q).automaticEnabled() ? 1 : 0);
        if (pool != address(0)) {
            (uint160 sqrt, int24 tick,,,,,) = ILiveV3Pool(pool).slot0();
            emit log_named_uint("v3_sqrt", sqrt);
            emit log_named_uint("v3_tick", uint256(int256(tick)));
            emit log_named_uint("v3_liquidity", ILiveV3Pool(pool).liquidity());
            emit log_named_uint("v3_usdg_balance", ILiveToken(USDG).balanceOf(pool));
            emit log_named_uint("v3_q_balance", ILiveToken(Q).balanceOf(pool));
        }
    }

    function run() external {
        uint256 usdg = ILiveToken(USDG).balanceOf(OWNER);
        uint256 lp = usdg * 3 / 4;
        uint256 buy = usdg - lp;
        uint256 target = (lp * 104 + 99) / 100;
        uint256 walletEth = OWNER.balance;
        uint256 ethCap = walletEth / 4;
        bytes32 id = keccak256(abi.encode(address(0), Q, uint24(2500), int24(25), address(0)));
        (uint160 v4Sqrt, int24 v4Tick,, uint24 v4Fee) = ILiveStateView(V4_STATE_VIEW).getSlot0(id);
        uint128 v4Liq = ILiveStateView(V4_STATE_VIEW).getLiquidity(id);
        address existing = ILiveFactory(V3_FACTORY).getPool(Q, USDG, 3000);
        emit log_named_uint("block", block.number);
        emit log_named_uint("timestamp", block.timestamp);
        emit log_named_uint("wallet_eth", walletEth);
        emit log_named_uint("eth_cap", ethCap);
        emit log_named_uint("wallet_usdg", usdg);
        emit log_named_uint("lp_usdg", lp);
        emit log_named_uint("buy_usdg", buy);
        emit log_named_uint("target_usdg_quote", target);
        emit log_named_uint("v4_sqrt", v4Sqrt);
        emit log_named_uint("v4_tick_encoded", uint256(int256(v4Tick)));
        emit log_named_uint("v4_fee", v4Fee);
        emit log_named_uint("v4_liquidity", v4Liq);
        emit log_named_address("existing_q_usdg_v3", existing);
        require(v4Sqrt != 0 && v4Fee == 2500 && v4Liq != 0 && existing == address(0), "pool preflight");
        uint256 lo = 1;
        uint256 hi = ethCap;
        require(_quoteUsdg(hi) >= target, "ETH cap cannot buy target USDG");
        while (lo < hi) {
            uint256 mid = lo + (hi - lo) / 2;
            if (_quoteUsdg(mid) >= target) hi = mid;
            else lo = mid + 1;
        }
        uint256 ethIn = lo;
        uint256 quoteUsdG = _quoteUsdg(ethIn);
        (uint256 quoteQ,) = ILiveV4Quoter(V4_QUOTER).quoteExactInputSingle(
            ILiveV4Quoter.QuoteExactSingleParams({
                poolKey: ILiveV4Quoter.PoolKey(address(0), Q, 2500, 25, address(0)),
                zeroForOne: true, exactAmount: uint128(ethIn), hookData: ""
            })
        );
        uint256 qLp = quoteQ * lp / quoteUsdG;
        emit log_named_uint("eth_in", ethIn);
        emit log_named_uint("quote_usdg", quoteUsdG);
        emit log_named_uint("quote_q", quoteQ);
        emit log_named_uint("q_for_lp", qLp);
        emit log_named_uint("min_q_v4_98pct", quoteQ * 98 / 100);
        emit log_named_uint("min_q_lp_95pct", qLp * 95 / 100);
        emit log_named_uint("min_usdg_lp_95pct", lp * 95 / 100);
        emit log_named_uint("buy_q_spot_95pct", buy * quoteQ * 997 * 95 / (quoteUsdG * 1000 * 100));
        uint256 numerator = Q < USDG ? quoteUsdG : quoteQ;
        uint256 denominator = Q < USDG ? quoteQ : quoteUsdG;
        uint256 ratioX192 = ((numerator / denominator) << 192)
            + ((numerator % denominator) << 192) / denominator;
        uint160 initialSqrt = uint160(_sqrt(ratioX192));
        emit log_named_uint("initial_sqrt", initialSqrt);
        emit log_named_uint("q_is_token0", Q < USDG ? 1 : 0);
    }

    function _quoteUsdg(uint256 ethIn) private returns (uint256 out) {
        (out,,,) = ILiveV3Quoter(V3_QUOTER).quoteExactInputSingle(
            ILiveV3Quoter.QuoteExactInputSingleParams(WETH, USDG, ethIn, 100, 0)
        );
    }

    function _sqrt(uint256 a) private pure returns (uint256) {
        if (a == 0) return 0;
        uint256 x = a;
        uint256 y = (x + 1) / 2;
        while (y < x) { x = y; y = (x + a / x) / 2; }
        return x;
    }
}
