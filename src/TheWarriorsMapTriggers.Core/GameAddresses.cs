// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 mostdak1ng

namespace TheWarriorsMapTriggers;

public static class GameAddresses
{
    public const uint CameraObjectPointer = 0x005D9148;
    public const uint CameraPriorityPointer = 0x005D9150;
    public const uint FollowCameraPointer = 0x005D9158;
    public const uint FollowCameraVtable = 0x00535D50;
    public const uint StartLockCameraVtable = 0x005362D0;
    public const uint CameraTransformOffset = 0x10;
    public const uint CameraVtableSignatureOffset = 0x10;
    public const int CameraVtableSignatureSize = 0x50;

    public const uint PlayerManagerPointer = 0x0051489C;
    public const uint EntityRegistry = 0x006EBD38;
    public const uint HumanVtable = 0x0053F088;
    public const uint MoveTransformBase = 0x00714B00;
    public const uint MoveTransformStride = 0x20;
    public const uint HumanMoveIndexOffset = 0x92;
    public const uint HumanStatePointerOffset = 0xD4;
    public const uint HumanObjectTransformOffset = 0x10;
    public const uint HumanLocalPositionOffset = 0x2C0;
    public static readonly uint[] HumanVerticalSpeedOffsets = [0x38, 0x3A0];

    public const ulong LocalPositionFlags64 = 0x0000001C00000000;
    public const uint LocalPositionFlags32 = 0x00000040;
    public const ulong PlayerGodModeBit = 0x0000000000000010;
    public const ulong PlayerNoTargetBit = 0x0000100000000000;
    public const ulong PlayerSafetyMask = PlayerGodModeBit | PlayerNoTargetBit;

    public const uint WorldTimestep = 0x005102CC;

    public const uint PadRefreshCall = 0x001498E0;
    public const uint PadRefreshOriginal = 0x0C0525DC;
    public const uint AllocatorBootstrapScratch = 0x0050A810;
    public const int AllocatorBootstrapScratchSize = 0x70;

    public const uint MainViewHook = 0x00185BFC;
    public const uint MainViewHookOriginal = 0x0C11C754;
    public const uint CameraBeginUpdateHook = 0x00198948;
    public const uint CameraBeginUpdateOriginal = 0x0C1209EE;
    public const uint BootstrapHook = 0x001567C4;
    public const uint BootstrapHookOriginal = 0x0C05F0C2;

    public static bool IsPlausibleEePointer(uint pointer) =>
        pointer is >= 0x00600000 and < 0x02000000;

    public static bool IsPlausibleHeapPointer(uint pointer) =>
        pointer is >= 0x00700000 and < 0x01FF0000;
}
