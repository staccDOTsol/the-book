// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Script.sol";
import {Orchestrator} from "../src/Orchestrator.sol";

/// @title DeployMined — CREATE2 mine the address until permission bits = 0x3F00
contract DeployMinedFactory {
    address constant POOL_MANAGER = 0x8366a39CC670B4001A1121B8F6A443A643e40951;
    address constant QUOTE_TOKEN = 0x0000000000000000000000000000000000000000;

    // required permission bits in the lower 14 bits of the hook address
    uint160 constant NEEDED = 0x3F00;
    //  = BEFORE_INITIALIZE (1<<13) | AFTER_INITIALIZE (1<<12)
    //  | BEFORE_ADD_LIQ (1<<11)   | AFTER_ADD_LIQ (1<<10)
    //  | BEFORE_REMOVE_LIQ (1<<9) | AFTER_REMOVE_LIQ... wait

    // Actually the v4 bits are:
    //  BEFORE_SWAP_FLAG      = 1 << 13 = 0x2000
    //  AFTER_SWAP_FLAG       = 1 << 12 = 0x1000
    //  BEFORE_ADD_LIQ_FLAG   = 1 << 11 = 0x0800
    //  AFTER_ADD_LIQ_FLAG    = 1 << 10 = 0x0400
    //  BEFORE_REMOVE_LIQ     = 1 << 9  = 0x0200
    //  AFTER_REMOVE_LIQ      = 1 << 8  = 0x0100
    //  BEFORE_INITIALIZE     = 1 << 7  = 0x0080
    //  AFTER_INITIALIZE      = 1 << 6  = 0x0040
    //  BEFORE_DONATE        = 1 << 5  = 0x0020
    //  AFTER_DONATE         = 1 << 4  = 0x0010
    //  BEFORE_SWAP_RETURNS_DELTA = 1 << 3 = 0x0008
    //  AFTER_SWAP_RETURNS_DELTA  = 1 << 2 = 0x0004
    //  AFTER_ADD_LIQ_RETURNS_DELTA = 1 << 1 = 0x0002
    //  AFTER_REMOVE_LIQ_RETURNS_DELTA = 1 << 0 = 0x0001

    // We need: beforeInitialize, afterInitialize, beforeAddLiq, afterAddLiq,
    //          beforeRemoveLiq, afterRemoveLiq, beforeSwap, afterSwap
    //  = 0x0080 | 0x0040 | 0x0800 | 0x0400 | 0x0200 | 0x0100 | 0x2000 | 0x1000
    //  = 0x3FC0

    // A factory that deploys child contracts via CREATE2
    function deployOrchestrator(bytes32 salt) external returns (address) {
        return address(new Orchestrator{salt: salt}(POOL_MANAGER, QUOTE_TOKEN, msg.sender));
    }

    }

contract DeployScript is Script {
    DeployMinedFactory factory;

    function run() public {
        uint256 deployerKey = vm.envUint("DEPLOYER_KEY");
        vm.startBroadcast(deployerKey);

        factory = new DeployMinedFactory();
        console.log("Factory deployed at:", address(factory));

        // mine salt
        bytes memory initcode = abi.encodePacked(
            type(Orchestrator).creationCode,
            abi.encode(0x8366a39CC670B4001A1121B8F6A443A643e40951, address(0))
        );
        bytes32 initcodeHash = keccak256(initcode);

        uint256 salt = 0;
        address predicted;
        bool found = false;

        while (salt < 5_000_000 && !found) {
            predicted = address(uint160(uint256(keccak256(abi.encodePacked(
                bytes1(0xff), address(factory), bytes32(salt), initcodeHash
            )))));

            if (uint160(predicted) & 0x3FFF == 0x3FC0) {
                found = true;
                break;
            }
            salt++;
        }

        require(found, "no salt found");
        console.log("Mined salt:", salt);
        console.log("Predicted address:", predicted);

        // deploy with mined salt
        address orchAddr = factory.deployOrchestrator(bytes32(salt));
        console.log("Orchestrator deployed at:", orchAddr);
        console.log("PERMISSIONS OK:", uint160(orchAddr) & 0x3FFF == 0x3FC0);

        vm.stopBroadcast();
    }
}
