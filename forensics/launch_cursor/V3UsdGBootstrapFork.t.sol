// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {HooklessLPExecutor} from "./HooklessLPExecutor.sol";
import {PositionInspector} from "./PositionInspector.sol";
import {LaunchCursorToken} from "./LaunchCursorToken.sol";
import {LaunchCursorTokenV2} from "./LaunchCursorTokenV2.sol";
import {HooklessQuoteBuyAdapter} from "./HooklessQuoteBuyAdapter.sol";
import {V3UsdGBootstrap, IV3BootstrapToken, IV3BootstrapFactory, IV3BootstrapPool,
    IV3BootstrapPositionManager} from "./V3UsdGBootstrap.sol";

interface IV3BootstrapForkVm {
    function deal(address who, uint256 newBalance) external;
    function prank(address sender) external;
    function skip(bool shouldSkip) external;
    function expectRevert() external;
}

interface IV3BootstrapForkPermit2 {
    function approve(address token, address spender, uint160 amount, uint48 expiration) external;
}

interface IV3BootstrapForkLauncher {
    struct Distribution {
        address strategy;
        uint128 amount;
        bytes configData;
    }

    function depositToken(address token, uint160 amount) external payable;
    function distributeToken(address token, Distribution calldata distribution, bytes32 salt) external payable;
    function multicall(bytes[] calldata data) external payable returns (bytes[] memory);
}

/// @notice No broadcast. A replacement Q first launches through the deployed
/// Pools.xyz Instant strategy on the fork; the bootstrap then creates its
/// second Q/USDG pool through deployed Uniswap v3 periphery in one call.
contract V3UsdGBootstrapForkTest {
    event log_named_uint(string key, uint256 val);

    IV3BootstrapForkVm private constant vm =
        IV3BootstrapForkVm(address(uint160(uint256(keccak256("hevm cheat code")))));
    uint256 private constant SUPPLY = 1_000_000_000 ether;
    address private constant V4_MANAGER = 0x8366a39CC670B4001A1121B8F6A443A643e40951;
    address private constant V4_POSITION_MANAGER = 0x58daec3116aae6D93017bAAea7749052E8a04fA7;
    address private constant V4_STATE_VIEW = 0xF3334192D15450CdD385c8B70e03f9A6bD9E673b;
    address private constant PERMIT2 = 0x000000000022D473030F116dDEE9F6B43aC78BA3;
    address private constant PONS_FACTORY = 0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e;
    address private constant LAUNCHER = 0x0000FffFBE8efE702c8703aE3477FF5dE3d319C0;
    address private constant INSTANT = 0x7c48DDe3B447381F4d986334679b3Afc7F2D35C2;
    address private constant V3_FACTORY = 0x1f7d7550B1b028f7571E69A784071F0205FD2EfA;
    address private constant V3_NPM = 0x73991a25C818Bf1f1128dEAaB1492D45638DE0D3;
    address private constant USDG = 0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168;
    address private constant WETH = 0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73;
    string private constant IMAGE_URI =
        "https://raw.githubusercontent.com/staccDOTsol/the-book/450d558b169408de1483c0540faa1aae72889a57/assets/wizard-token.png";

    LaunchCursorTokenV2 private q;
    V3UsdGBootstrap private bootstrapper;

    function setUp() external {
        if (block.chainid != 4663) return;
        vm.deal(address(this), 1 ether);

        HooklessLPExecutor executor = new HooklessLPExecutor(
            V4_MANAGER, V4_POSITION_MANAGER, V4_STATE_VIEW, PERMIT2
        );
        PositionInspector inspector = new PositionInspector(address(executor));
        address[] memory endpoints = new address[](4);
        endpoints[0] = PERMIT2;
        endpoints[1] = LAUNCHER;
        endpoints[2] = INSTANT;
        endpoints[3] = V4_POSITION_MANAGER;
        q = new LaunchCursorTokenV2(
            LaunchCursorToken.Metadata(unicode"🧙‍♂️", unicode"🧙‍♂️", unicode"🧙‍♂️", IMAGE_URI),
            SUPPLY, PONS_FACTORY, address(executor), address(inspector),
            30, 3_000_000, 1 gwei, endpoints,
            LaunchCursorTokenV2.V3PoolConfig(V3_FACTORY, USDG, 3_000)
        );
        executor.bindController(address(q));
        inspector.bindCursor(address(q));
        q.setAutomatic(false);

        _launchInstant();
        q.setPriceConfigurator(address(0xA11CE));
        q.setExitConfigurator(address(0xE417));
        q.setAutomatic(true);
        bootstrapper = new V3UsdGBootstrap(address(q), address(this));
        _fundUsdG();
        IV3BootstrapToken(USDG).approve(address(bootstrapper), 201_000_000);
    }

    function _launchInstant() private {
        q.approve(PERMIT2, SUPPLY);
        IV3BootstrapForkPermit2(PERMIT2).approve(
            address(q), LAUNCHER, uint160(SUPPLY), uint48(block.timestamp + 1 hours)
        );
        IV3BootstrapForkLauncher.Distribution memory d = IV3BootstrapForkLauncher.Distribution({
            strategy: INSTANT, amount: uint128(SUPPLY), configData: abi.encode(address(0xBEEF))
        });
        bytes[] memory calls = new bytes[](2);
        calls[0] = abi.encodeCall(IV3BootstrapForkLauncher.depositToken, (address(q), uint160(SUPPLY)));
        calls[1] = abi.encodeCall(IV3BootstrapForkLauncher.distributeToken, (address(q), d, bytes32(0)));
        IV3BootstrapForkLauncher(LAUNCHER).multicall(calls);
        require(q.balanceOf(address(this)) == 0, "Pools launch did not consume Q");
    }

    function _fundUsdG() private {
        address sourcePool = IV3BootstrapFactory(V3_FACTORY).getPool(USDG, WETH, 3_000);
        require(sourcePool != address(0) && IV3BootstrapToken(USDG).balanceOf(sourcePool) >= 201_000_000,
            "fork USDG source unavailable");
        vm.prank(sourcePool);
        require(IV3BootstrapToken(USDG).transfer(address(this), 201_000_000), "fork USDG funding failed");
    }

    function _plan() private view returns (V3UsdGBootstrap.Plan memory p) {
        // 1:1 raw-unit start is a deliberately synthetic mechanics fixture,
        // not a Q/USDG valuation or suggested live launch price.
        uint160 limit = USDG < address(q)
            ? uint160(4_295_128_740)
            : uint160(1_461_446_703_485_210_103_287_273_052_203_988_822_378_723_970_341);
        p = V3UsdGBootstrap.Plan({
            qInventoryIn: 0, minQFromV4: 100_000_000,
            qForLp: 100_000_000, usdgForLp: 200_000_000,
            minQInLp: 1_000_000, minUsdgInLp: 1_000_000,
            usdgForBuy: 1_000_000, minQFromV3Buy: 1,
            initialSqrtPriceX96: uint160(1 << 96),
            v3BuySqrtPriceLimitX96: limit,
            tickLower: -600, tickUpper: 600,
            deadline: uint64(block.timestamp + 10 minutes)
        });
    }

    function testPoolsInstantThenAtomicSecondV3Pool() external {
        vm.skip(block.chainid != 4663);
        V3UsdGBootstrap.Plan memory p = _plan();
        uint256 usdGBefore = IV3BootstrapToken(USDG).balanceOf(address(this));
        uint256 gasBefore = gasleft();
        (address pool, uint256 tokenId, uint256 qBought) = bootstrapper.bootstrap{value: 0.01 ether}(p);
        uint256 bootstrapGas = gasBefore - gasleft();
        require(pool == IV3BootstrapFactory(V3_FACTORY).getPool(address(q), USDG, 3_000), "wrong pool");
        require(q.canonicalV3Pool() == pool, "Q trigger was not bound");
        require(IV3BootstrapPool(pool).liquidity() > 0, "LP inactive");
        require(IV3BootstrapPositionManager(V3_NPM).ownerOf(tokenId) == address(this), "wrong NFT owner");
        require(qBought > 0 && q.balanceOf(address(this)) >= qBought, "no first buy");
        require(IV3BootstrapToken(USDG).balanceOf(address(bootstrapper)) == 0, "USDG not refunded");
        require(q.balanceOf(address(bootstrapper)) == 0, "Q not refunded");
        require(IV3BootstrapToken(USDG).balanceOf(address(this)) < usdGBefore, "USDG was not spent");
        emit log_named_uint("atomic v3 bootstrap gas", bootstrapGas);
    }

    function testUnfillableFirstBuyRollsBackPoolMintAndBinding() external {
        vm.skip(block.chainid != 4663);
        V3UsdGBootstrap.Plan memory p = _plan();
        p.minQFromV3Buy = type(uint256).max;
        uint256 usdGBefore = IV3BootstrapToken(USDG).balanceOf(address(this));
        vm.expectRevert();
        bootstrapper.bootstrap{value: 0.01 ether}(p);
        require(IV3BootstrapFactory(V3_FACTORY).getPool(address(q), USDG, 3_000) == address(0),
            "failed call left pool");
        require(q.canonicalV3Pool() == address(0), "failed call left trigger bound");
        require(IV3BootstrapToken(USDG).balanceOf(address(this)) == usdGBefore, "failed call spent USDG");
    }

    function testProvidedQInventoryCanSeedSecondPool() external {
        vm.skip(block.chainid != 4663);
        HooklessQuoteBuyAdapter inventoryBuyer = new HooklessQuoteBuyAdapter(
            V4_MANAGER, address(q), 2_500, 25, address(this)
        );
        uint256 inventory = inventoryBuyer.buyQ{value: 0.01 ether}(
            100_000_000, address(this), uint64(block.timestamp + 10 minutes)
        );
        q.approve(address(bootstrapper), inventory);
        V3UsdGBootstrap.Plan memory p = _plan();
        p.qInventoryIn = inventory;
        p.minQFromV4 = 0;
        (address pool, uint256 tokenId, uint256 bought) = bootstrapper.bootstrap(p);
        require(pool == q.canonicalV3Pool() && bought > 0, "inventory setup failed");
        require(IV3BootstrapPositionManager(V3_NPM).ownerOf(tokenId) == address(this), "wrong NFT owner");
        require(q.balanceOf(address(bootstrapper)) == 0, "inventory refund retained");
    }
}
