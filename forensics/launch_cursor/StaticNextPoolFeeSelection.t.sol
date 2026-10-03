// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {StaticNextPoolFee} from "./StaticNextPoolFee.sol";

contract StaticNextPoolFeeSelectionTest {
    function testUnresolvedAndCensoredAssignmentsRemainInSelectionScore() external {
        StaticNextPoolFee policy = new StaticNextPoolFee(address(this));
        address first = address(0xA1);
        address second = address(0xA2);

        policy.selectFee(first);
        (, uint8 firstArm,) = policy.assignments(first);
        (uint64 count, int256 sum, int256 mean) = policy.selectionScore(firstArm);
        require(count == 1 && sum == -5_000 && mean == -5_000, "open arm disappeared");

        policy.recordClosed(first, 1_000);
        (count, sum, mean) = policy.selectionScore(firstArm);
        require(count == 1 && sum == 1_000 && mean == 1_000, "closed score not substituted");

        policy.selectFee(second);
        (, uint8 secondArm,) = policy.assignments(second);
        (count, sum, mean) = policy.selectionScore(secondArm);
        if (secondArm == firstArm) {
            require(count == 2 && sum == -4_000 && mean == -2_000, "idle token omitted");
        } else {
            require(count == 1 && sum == -5_000 && mean == -5_000, "idle arm omitted");
        }

        policy.recordCensored(second, StaticNextPoolFee.CensorReason.UnvaluedExit);
        (count, sum, mean) = policy.selectionScore(secondArm);
        if (secondArm == firstArm) {
            require(count == 2 && sum == -4_000 && mean == -2_000, "censor escaped denominator");
        } else {
            require(count == 1 && sum == -5_000 && mean == -5_000, "censored arm disappeared");
        }
        require(policy.armAssignments(secondArm) >= 1, "assignment not counted");
    }
}
