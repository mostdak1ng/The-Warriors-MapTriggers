// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 mostdak1ng

using System.Buffers.Binary;
using System.Net.Sockets;
using System.Numerics;

namespace TheWarriorsMapTriggers;

public sealed class PlayerController : IDisposable
{
    private readonly PineClient client;
    private ulong originalSafetyBits;
    private bool disposed;

    public PlayerController(PineClient client)
    {
        this.client = client;
        GodModeEnabled = true;
        Refresh();
    }

    public uint Human { get; private set; }

    public uint EntityHandle { get; private set; }

    public uint? MovementAddress { get; private set; }

    public bool Attached { get; private set; }

    public bool GodModeEnabled { get; private set; }

    public int PositionWrites { get; private set; }

    public int GravityResets { get; private set; }

    public Vector3? LastPosition { get; private set; }

    public static uint ResolvePlayerObject(PineClient client)
    {
        (uint human, _) = ResolvePlayerEntity(client);
        return human;
    }

    private static (uint Human, uint Handle) ResolvePlayerEntity(PineClient client)
    {
        uint manager = client.Read32(GameAddresses.PlayerManagerPointer);
        if (!GameAddresses.IsPlausibleEePointer(manager))
        {
            throw new WorldUnavailableException(
                $"Invalid player manager pointer: 0x{manager:X8}.");
        }

        uint handle = client.Read32(manager + 0x228);
        if (handle == uint.MaxValue)
        {
            throw new WorldUnavailableException("Player 1 has no live entity handle.");
        }

        uint generation = handle & 0xFFFF;
        uint index = handle >> 16;
        uint registryEntry = checked(GameAddresses.EntityRegistry + (index * 8));
        if (client.Read16(registryEntry + 4) != generation)
        {
            throw new WorldUnavailableException(
                "Player entity handle generation does not match the registry.");
        }

        uint human = client.Read32(registryEntry);
        if (!GameAddresses.IsPlausibleEePointer(human))
        {
            throw new WorldUnavailableException(
                $"Invalid player object pointer: 0x{human:X8}.");
        }

        if (client.Read32(human) != GameAddresses.HumanVtable)
        {
            throw new WorldUnavailableException(
                $"Player object 0x{human:X8} is not a live Human.");
        }

        return (human, handle);
    }

    public static uint? ResolveMovementAddress(PineClient client, uint human)
    {
        short moveIndex = unchecked((short)client.Read16(
            human + GameAddresses.HumanMoveIndexOffset));
        if (moveIndex is < 0 or >= 0x4000)
        {
            return null;
        }

        uint address = checked(
            GameAddresses.MoveTransformBase +
            ((uint)moveIndex * GameAddresses.MoveTransformStride));
        return address <= 0x01FFFFFF - GameAddresses.MoveTransformStride
            ? address
            : null;
    }

    public static Vector3 ReadPlayerPosition(PineClient client, uint? human = null)
    {
        uint resolvedHuman = human ?? ResolvePlayerObject(client);
        uint positionAddress = ResolveEffectivePositionAddress(client, resolvedHuman);
        byte[] block = client.ReadBlock(positionAddress, 0x10);
        var position = new Vector3(
            ReadFloat(block, 0x00),
            ReadFloat(block, 0x04),
            ReadFloat(block, 0x08));
        if (!CameraMath.IsFinite(position))
        {
            throw new WorldUnavailableException(
                "Player position contains non-finite values.");
        }

        return position;
    }

    public void Refresh()
    {
        ObjectDisposedException.ThrowIf(disposed, this);
        (uint human, uint handle) = ResolvePlayerEntity(client);
        if (human == Human && handle == EntityHandle)
        {
            MovementAddress = ResolveMovementAddress(client, human);
            ApplySafety();
            return;
        }

        TryRestorePreviousSafety();
        Human = human;
        EntityHandle = handle;
        MovementAddress = ResolveMovementAddress(client, human);
        originalSafetyBits = client.Read64(human + 0xE0) &
            GameAddresses.PlayerSafetyMask;
        ApplySafety();
    }

    public void SetGodMode(bool enabled)
    {
        GodModeEnabled = enabled;
        if (Human != 0)
        {
            Refresh();
        }
    }

    public bool ToggleGodMode()
    {
        SetGodMode(!GodModeEnabled);
        return GodModeEnabled;
    }

    public void SetAttached(bool enabled)
    {
        if (Attached == enabled)
        {
            return;
        }

        if (Human != 0)
        {
            Refresh();
            NeutralizeVerticalSpeed();
        }

        Attached = enabled;
        if (Human != 0)
        {
            ApplySafety();
        }
    }

    public Vector3 WriteAttachedPosition(
        Vector3 cameraPosition,
        Vector3 cameraForward,
        float forwardOffset = 3f,
        float upOffset = -1.5f)
    {
        if (!Attached)
        {
            throw new InvalidOperationException("Player carry is not active.");
        }

        Refresh();
        Vector3 target = cameraPosition +
            (cameraForward * forwardOffset) +
            (Vector3.UnitZ * upOffset);
        if (!CameraMath.IsFinite(target))
        {
            throw new InvalidDataException("Player carry target is not finite.");
        }

        Span<byte> block = stackalloc byte[0x10];
        WriteFloat(block, 0x00, target.X);
        WriteFloat(block, 0x04, target.Y);
        WriteFloat(block, 0x08, target.Z);
        WriteFloat(block, 0x0C, 1f);

        if (MovementAddress is uint movement)
        {
            client.WriteBlock(movement, block);
        }

        client.WriteBlock(Human + GameAddresses.HumanObjectTransformOffset, block);
        client.WriteBlock(Human + GameAddresses.HumanLocalPositionOffset, block);
        NeutralizeVerticalSpeed();
        LastPosition = target;
        PositionWrites++;
        return target;
    }

    public Vector3 ReadPosition()
    {
        Refresh();
        Vector3 position = ReadPlayerPosition(client, Human);
        LastPosition = position;
        return position;
    }

    public void AbandonWorld()
    {
        Human = 0;
        EntityHandle = 0;
        MovementAddress = null;
        originalSafetyBits = 0;
        Attached = false;
        LastPosition = null;
    }

    public void SuspendWorld()
    {
        Attached = false;
        try
        {
            if (Human != 0 && IsCurrentEntity(Human, EntityHandle))
            {
                ApplySafety();
            }
        }
        catch (Exception error) when (
            error is IOException or SocketException or InvalidOperationException)
        {
            // The player may already be gone during a loading screen.
        }

        MovementAddress = null;
        LastPosition = null;
    }

    public void Dispose()
    {
        if (disposed)
        {
            return;
        }

        try
        {
            if (Human != 0)
            {
                bool wasAttached = Attached;
                Attached = false;
                if (IsCurrentEntity(Human, EntityHandle))
                {
                    if (wasAttached)
                    {
                        NeutralizeVerticalSpeed();
                    }

                    RestoreSafety(Human, originalSafetyBits);
                }
            }
        }
        finally
        {
            disposed = true;
            Human = 0;
            EntityHandle = 0;
            MovementAddress = null;
        }
    }

    private static uint ResolveEffectivePositionAddress(
        PineClient client, uint human)
    {
        uint state = client.Read32(human + GameAddresses.HumanStatePointerOffset);
        if (GameAddresses.IsPlausibleEePointer(state))
        {
            ulong flags64 = client.Read64(state);
            uint flags32 = client.Read32(state + 8);
            if ((flags64 & GameAddresses.LocalPositionFlags64) != 0 ||
                (flags32 & GameAddresses.LocalPositionFlags32) != 0)
            {
                return human + GameAddresses.HumanLocalPositionOffset;
            }
        }

        return ResolveMovementAddress(client, human) ??
            throw new WorldUnavailableException(
                "Player has no valid movement transform.");
    }

    private void ApplySafety()
    {
        if (Human == 0)
        {
            return;
        }

        ulong current = client.Read64(Human + 0xE0);
        ulong target = current & ~GameAddresses.PlayerSafetyMask;
        target |= originalSafetyBits & GameAddresses.PlayerNoTargetBit;
        if (GodModeEnabled)
        {
            target |= GameAddresses.PlayerGodModeBit;
        }

        if (Attached)
        {
            target |= GameAddresses.PlayerNoTargetBit;
        }

        if (target != current)
        {
            client.Write64(Human + 0xE0, target);
        }
    }

    private void NeutralizeVerticalSpeed()
    {
        if (Human == 0)
        {
            return;
        }

        foreach (uint offset in GameAddresses.HumanVerticalSpeedOffsets)
        {
            client.Write32(Human + offset, 0);
        }

        GravityResets++;
    }

    private void TryRestorePreviousSafety()
    {
        if (Human == 0)
        {
            return;
        }

        try
        {
            if (IsCurrentEntity(Human, EntityHandle))
            {
                RestoreSafety(Human, originalSafetyBits);
            }
        }
        catch (Exception error) when (
            error is IOException or SocketException or InvalidOperationException)
        {
            // A loading screen may already have destroyed the previous entity.
        }
    }

    private bool IsCurrentEntity(uint human, uint handle)
    {
        if (!GameAddresses.IsPlausibleEePointer(human) ||
            client.Read32(human) != GameAddresses.HumanVtable)
        {
            return false;
        }

        uint generation = handle & 0xFFFF;
        uint index = handle >> 16;
        uint registryEntry = checked(GameAddresses.EntityRegistry + (index * 8));
        return client.Read16(registryEntry + 4) == generation &&
            client.Read32(registryEntry) == human;
    }

    private void RestoreSafety(uint human, ulong originalBits)
    {
        ulong current = client.Read64(human + 0xE0);
        ulong restored = (current & ~GameAddresses.PlayerSafetyMask) |
            (originalBits & GameAddresses.PlayerSafetyMask);
        if (restored != current)
        {
            client.Write64(human + 0xE0, restored);
        }
    }

    private static float ReadFloat(ReadOnlySpan<byte> source, int offset) =>
        BitConverter.Int32BitsToSingle(
            BinaryPrimitives.ReadInt32LittleEndian(source[offset..]));

    private static void WriteFloat(Span<byte> destination, int offset, float value) =>
        BinaryPrimitives.WriteInt32LittleEndian(
            destination[offset..], BitConverter.SingleToInt32Bits(value));
}
