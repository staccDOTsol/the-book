// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Script.sol";
import {Orchestrator} from "../src/Orchestrator.sol";

contract Deploy is Script {
    address constant POOL_MANAGER = 0x8366a39CC670B4001A1121B8F6A443A643e40951;
    address constant QUOTE_TOKEN = 0x0000000000000000000000000000000000000000; // native ETH

    function run() public {
        uint256 deployerKey = vm.envUint("DEPLOYER_KEY");
        vm.startBroadcast(deployerKey);

        Orchestrator orch = new Orchestrator(
            POOL_MANAGER,
            QUOTE_TOKEN,
            msg.sender
        );

        console.log("DEPLOYED AT:", address(orch));
        console.log("DEPLOYER:", msg.sender);

        vm.stopBroadcast();
    }
}
