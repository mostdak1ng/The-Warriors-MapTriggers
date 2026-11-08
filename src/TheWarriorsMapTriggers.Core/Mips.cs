// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 mostdak1ng

namespace TheWarriorsMapTriggers;

public static class Mips
{
    public static uint IType(int opcode, int source, int target, int immediate) =>
        ((uint)opcode << 26) |
        ((uint)source << 21) |
        ((uint)target << 16) |
        ((uint)immediate & 0xFFFF);

    public static uint AddImmediateUnsigned(int target, int source, int immediate) =>
        IType(0x09, source, target, immediate);

    public static uint LoadUpperImmediate(int target, int immediate) =>
        IType(0x0F, 0, target, immediate);

    public static uint OrImmediate(int target, int source, int immediate) =>
        IType(0x0D, source, target, immediate);

    public static uint LoadWord(int target, int offset, int source) =>
        IType(0x23, source, target, offset);

    public static uint LoadDoubleword(int target, int offset, int source) =>
        IType(0x37, source, target, offset);

    public static uint StoreHalfword(int target, int offset, int source) =>
        IType(0x29, source, target, offset);

    public static uint StoreWord(int target, int offset, int source) =>
        IType(0x2B, source, target, offset);

    public static uint StoreDoubleword(int target, int offset, int source) =>
        IType(0x3F, source, target, offset);

    public static uint BranchEqual(int first, int second, int displacementWords) =>
        IType(0x04, first, second, displacementWords);

    public static uint JumpAndLink(uint address) =>
        0x0C000000 | ((address >> 2) & 0x03FFFFFF);

    public static uint JumpRegister(int register) => ((uint)register << 21) | 0x08;
}
