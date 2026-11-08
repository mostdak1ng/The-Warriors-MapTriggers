// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 mostdak1ng

using System.Buffers.Binary;
using System.Numerics;

namespace TheWarriorsMapTriggers;

public sealed class CameraController
{
    private readonly PineClient client;
    private readonly Dictionary<uint, bool> compatibleVtables = new()
    {
        [GameAddresses.FollowCameraVtable] = true,
        [GameAddresses.StartLockCameraVtable] = true,
    };
    private byte[]? referenceVtableSignature;

    public CameraController(PineClient client)
    {
        this.client = client;
        Rebind(preservePose: false);
    }

    public uint CameraObject { get; private set; }

    public uint TransformAddress { get; private set; }

    public Vector3 Position { get; private set; }

    public float PositionW { get; private set; } = 1f;

    public Quaternion Orientation { get; private set; } = Quaternion.Identity;

    public uint Vtable { get; private set; }

    public bool Rebind(bool preservePose)
    {
        uint cameraObject = client.Read32(GameAddresses.CameraObjectPointer);
        uint transform = ResolveTransform(cameraObject, out uint vtable);
        bool changed = cameraObject != CameraObject;
        if (!changed && preservePose)
        {
            return false;
        }

        CameraPose livePose = ReadPose(transform);
        CameraObject = cameraObject;
        TransformAddress = transform;
        Vtable = vtable;
        PositionW = livePose.PositionW;
        if (!preservePose || !CameraMath.IsFinite(Position))
        {
            Position = livePose.Position;
            Orientation = livePose.Orientation;
        }

        return changed;
    }

    public void ReadNormalPose()
    {
        Rebind(preservePose: false);
    }

    public CameraHandoff HandOffToFollow()
    {
        uint previousActive = client.Read32(GameAddresses.CameraObjectPointer);
        uint previousPriority = client.Read32(GameAddresses.CameraPriorityPointer);
        uint follow = client.Read32(GameAddresses.FollowCameraPointer);
        _ = ResolveTransform(follow, out uint followVtable);
        if (followVtable != GameAddresses.FollowCameraVtable)
        {
            throw new WorldUnavailableException(
                $"Camera slot 0x{GameAddresses.FollowCameraPointer:X8} does not " +
                $"contain the player FollowCamera (object 0x{follow:X8}, " +
                $"vtable 0x{followVtable:X8}).");
        }

        client.Write32Pair(
            GameAddresses.CameraObjectPointer,
            follow,
            GameAddresses.CameraPriorityPointer,
            follow);
        uint active = client.Read32(GameAddresses.CameraObjectPointer);
        uint priority = client.Read32(GameAddresses.CameraPriorityPointer);
        if (active != follow || priority != follow)
        {
            throw new InvalidOperationException(
                "The game camera manager did not accept the FollowCamera handoff: " +
                $"expected 0x{follow:X8}, active 0x{active:X8}, " +
                $"priority 0x{priority:X8}.");
        }

        Rebind(preservePose: false);
        return new CameraHandoff(previousActive, previousPriority, follow);
    }

    public void Move(Vector3 delta)
    {
        if (!CameraMath.IsFinite(delta))
        {
            throw new ArgumentException("Camera movement must be finite.", nameof(delta));
        }

        Position += delta;
    }

    public void Rotate(float yawRadians, float pitchRadians)
    {
        if (yawRadians != 0f)
        {
            Quaternion yaw = CameraMath.AxisAngle(Vector3.UnitZ, yawRadians);
            Orientation = CameraMath.Multiply(yaw, Orientation);
        }

        if (pitchRadians != 0f)
        {
            Quaternion pitch = CameraMath.AxisAngle(Vector3.UnitX, pitchRadians);
            Quaternion candidate = CameraMath.Multiply(Orientation, pitch);
            Vector3 candidateForward = CameraMath.Rotate(candidate, Vector3.UnitY);
            if (Math.Abs(candidateForward.Z) < 0.995f)
            {
                Orientation = candidate;
            }
        }
    }

    public Vector3 Right => Vector3.Normalize(
        CameraMath.Rotate(Orientation, Vector3.UnitX));

    public Vector3 Forward => Vector3.Normalize(
        CameraMath.Rotate(Orientation, Vector3.UnitY));

    public void WritePose()
    {
        uint liveObject = client.Read32(GameAddresses.CameraObjectPointer);
        if (liveObject != CameraObject)
        {
            Rebind(preservePose: true);
        }
        else
        {
            _ = ResolveTransform(liveObject, out _);
        }

        Span<byte> block = stackalloc byte[0x20];
        WriteFloat(block, 0x00, Position.X);
        WriteFloat(block, 0x04, Position.Y);
        WriteFloat(block, 0x08, Position.Z);
        WriteFloat(block, 0x0C, PositionW);
        WriteFloat(block, 0x10, Orientation.X);
        WriteFloat(block, 0x14, Orientation.Y);
        WriteFloat(block, 0x18, Orientation.Z);
        WriteFloat(block, 0x1C, Orientation.W);
        client.WriteBlock(TransformAddress, block);
    }

    public CameraPose ReadLivePose() => ReadPose(
        ResolveTransform(client.Read32(GameAddresses.CameraObjectPointer), out _));

    private uint ResolveTransform(uint cameraObject, out uint vtable)
    {
        if (!GameAddresses.IsPlausibleHeapPointer(cameraObject))
        {
            throw new WorldUnavailableException(
                $"Invalid active camera pointer: 0x{cameraObject:X8}.");
        }

        vtable = client.Read32(cameraObject);
        if (!IsCompatibleVtable(vtable))
        {
            throw new WorldUnavailableException(
                $"Camera object 0x{cameraObject:X8} has incompatible vtable " +
                $"0x{vtable:X8}.");
        }

        return checked(cameraObject + GameAddresses.CameraTransformOffset);
    }

    private bool IsCompatibleVtable(uint vtable)
    {
        if (compatibleVtables.TryGetValue(vtable, out bool compatible))
        {
            return compatible;
        }

        if (vtable is < 0x00100000 or >= 0x00700000)
        {
            compatibleVtables[vtable] = false;
            return false;
        }

        referenceVtableSignature ??= client.ReadBlock(
            GameAddresses.FollowCameraVtable +
            GameAddresses.CameraVtableSignatureOffset,
            GameAddresses.CameraVtableSignatureSize);
        byte[] candidate = client.ReadBlock(
            vtable + GameAddresses.CameraVtableSignatureOffset,
            GameAddresses.CameraVtableSignatureSize);
        compatible = candidate.AsSpan().SequenceEqual(referenceVtableSignature);
        compatibleVtables[vtable] = compatible;
        return compatible;
    }

    private CameraPose ReadPose(uint transform)
    {
        byte[] raw = client.ReadBlock(transform, 0x20);
        var position = new Vector3(
            ReadFloat(raw, 0x00),
            ReadFloat(raw, 0x04),
            ReadFloat(raw, 0x08));
        float positionW = ReadFloat(raw, 0x0C);
        var orientation = new Quaternion(
            ReadFloat(raw, 0x10),
            ReadFloat(raw, 0x14),
            ReadFloat(raw, 0x18),
            ReadFloat(raw, 0x1C));
        if (!CameraMath.IsFinite(position) || !float.IsFinite(positionW))
        {
            throw new WorldUnavailableException("Camera transform contains non-finite values.");
        }

        try
        {
            orientation = CameraMath.NormalizeQuaternion(orientation);
        }
        catch (InvalidDataException error)
        {
            throw new WorldUnavailableException("Camera orientation is unavailable.", error);
        }

        return new CameraPose(position, positionW, orientation);
    }

    private static float ReadFloat(ReadOnlySpan<byte> source, int offset) =>
        BitConverter.Int32BitsToSingle(
            BinaryPrimitives.ReadInt32LittleEndian(source[offset..]));

    private static void WriteFloat(Span<byte> destination, int offset, float value) =>
        BinaryPrimitives.WriteInt32LittleEndian(
            destination[offset..], BitConverter.SingleToInt32Bits(value));
}

public readonly record struct CameraPose(
    Vector3 Position,
    float PositionW,
    Quaternion Orientation);

public readonly record struct CameraHandoff(
    uint PreviousActiveCamera,
    uint PreviousPriorityCamera,
    uint FollowCamera);
