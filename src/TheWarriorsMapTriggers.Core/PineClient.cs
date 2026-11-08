// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 mostdak1ng

using System.Buffers.Binary;
using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using System.Text;

namespace TheWarriorsMapTriggers;

public enum PineOpcode : byte
{
    Read8 = 0,
    Read16 = 1,
    Read32 = 2,
    Read64 = 3,
    Write8 = 4,
    Write16 = 5,
    Write32 = 6,
    Write64 = 7,
    Version = 8,
    Title = 0x0B,
    GameId = 0x0C,
    Uuid = 0x0D,
    GameVersion = 0x0E,
    Status = 0x0F,
}

public enum PineStatus : uint
{
    Running = 0,
    Paused = 1,
    Shutdown = 2,
}

public sealed class PineClient : IDisposable
{
    private const int MaximumResponseBytes = 1_000_000;
    private readonly TcpClient tcpClient;
    private readonly NetworkStream stream;
    private readonly object requestLock = new();
    private bool disposed;

    private PineClient(TcpClient tcpClient, TimeSpan ioTimeout)
    {
        this.tcpClient = tcpClient;
        stream = tcpClient.GetStream();
        int timeoutMilliseconds = checked((int)ioTimeout.TotalMilliseconds);
        stream.ReadTimeout = timeoutMilliseconds;
        stream.WriteTimeout = timeoutMilliseconds;
    }

    public static PineClient Connect(
        int port = BuildInfo.DefaultPinePort,
        TimeSpan? connectTimeout = null,
        TimeSpan? ioTimeout = null)
    {
        TimeSpan connectLimit = connectTimeout ?? TimeSpan.FromSeconds(2);
        TimeSpan ioLimit = ioTimeout ?? TimeSpan.FromSeconds(3);
        var client = new TcpClient(AddressFamily.InterNetwork)
        {
            NoDelay = true,
        };

        using var cancellation = new CancellationTokenSource(connectLimit);
        try
        {
            client.ConnectAsync(IPAddress.Loopback, port, cancellation.Token)
                .AsTask()
                .GetAwaiter()
                .GetResult();
            return new PineClient(client, ioLimit);
        }
        catch
        {
            client.Dispose();
            throw;
        }
    }

    public string ReadText(PineOpcode opcode)
    {
        if (opcode is not (
            PineOpcode.Version or
            PineOpcode.Title or
            PineOpcode.GameId or
            PineOpcode.Uuid or
            PineOpcode.GameVersion))
        {
            throw new ArgumentOutOfRangeException(
                nameof(opcode), opcode, "Opcode is not a PINE text command.");
        }

        byte[] response = Request([(byte)opcode]);
        if (response.Length < sizeof(uint))
        {
            throw new InvalidDataException("PINE returned a truncated text response.");
        }

        uint requestedLength = BinaryPrimitives.ReadUInt32LittleEndian(response);
        int availableLength = Math.Min(
            checked((int)requestedLength), response.Length - sizeof(uint));
        return Encoding.UTF8
            .GetString(response, sizeof(uint), availableLength)
            .TrimEnd('\0');
    }

    public PineStatus ReadStatus()
    {
        byte[] response = Request([(byte)PineOpcode.Status]);
        if (response.Length != sizeof(uint))
        {
            throw new InvalidDataException(
                $"PINE status response had {response.Length} bytes.");
        }

        return (PineStatus)BinaryPrimitives.ReadUInt32LittleEndian(response);
    }

    public byte Read8(uint address) => checked((byte)ReadInteger(address, 8));

    public ushort Read16(uint address) => checked((ushort)ReadInteger(address, 16));

    public uint Read32(uint address) => checked((uint)ReadInteger(address, 32));

    public ulong Read64(uint address) => ReadInteger(address, 64);

    public void Write8(uint address, byte value) => WriteInteger(address, value, 8);

    public void Write16(uint address, ushort value) => WriteInteger(address, value, 16);

    public void Write32(uint address, uint value) => WriteInteger(address, value, 32);

    public void Write64(uint address, ulong value) => WriteInteger(address, value, 64);

    public void Write32Pair(
        uint firstAddress,
        uint firstValue,
        uint secondAddress,
        uint secondValue)
    {
        Span<byte> payload = stackalloc byte[18];
        payload[0] = (byte)PineOpcode.Write32;
        BinaryPrimitives.WriteUInt32LittleEndian(payload[1..], firstAddress);
        BinaryPrimitives.WriteUInt32LittleEndian(payload[5..], firstValue);
        payload[9] = (byte)PineOpcode.Write32;
        BinaryPrimitives.WriteUInt32LittleEndian(payload[10..], secondAddress);
        BinaryPrimitives.WriteUInt32LittleEndian(payload[14..], secondValue);
        byte[] response = Request(payload);
        if (response.Length != 0)
        {
            throw new InvalidDataException(
                $"PINE paired write returned {response.Length} unexpected bytes.");
        }
    }

    public byte[] ReadBlock(uint address, int size)
    {
        if ((address & 7) != 0 || size < 0 || (size & 7) != 0)
        {
            throw new ArgumentException(
                "PINE block reads require an eight-byte-aligned address and size.");
        }

        if (size == 0)
        {
            return [];
        }

        const int maximumValuesPerRequest = 50_000;
        byte[] result = new byte[size];
        int totalValues = size / sizeof(ulong);
        int completedValues = 0;

        while (completedValues < totalValues)
        {
            int valueCount = Math.Min(
                maximumValuesPerRequest, totalValues - completedValues);
            byte[] payload = new byte[valueCount * 5];
            for (int index = 0; index < valueCount; index++)
            {
                int payloadOffset = index * 5;
                payload[payloadOffset] = (byte)PineOpcode.Read64;
                uint valueAddress = checked(
                    address + (uint)((completedValues + index) * sizeof(ulong)));
                BinaryPrimitives.WriteUInt32LittleEndian(
                    payload.AsSpan(payloadOffset + 1, sizeof(uint)), valueAddress);
            }

            byte[] response = Request(payload);
            int expectedSize = valueCount * sizeof(ulong);
            if (response.Length != expectedSize)
            {
                throw new InvalidDataException(
                    $"PINE block read expected {expectedSize} bytes and received " +
                    $"{response.Length}.");
            }

            Buffer.BlockCopy(
                response, 0, result, completedValues * sizeof(ulong), expectedSize);
            completedValues += valueCount;
        }

        return result;
    }

    public void WriteBlock(uint address, ReadOnlySpan<byte> data)
    {
        if ((address & 7) != 0 || (data.Length & 7) != 0)
        {
            throw new ArgumentException(
                "PINE block writes require an eight-byte-aligned address and size.");
        }

        const int maximumValuesPerRequest = 20_000;
        int totalValues = data.Length / sizeof(ulong);
        int completedValues = 0;

        while (completedValues < totalValues)
        {
            int valueCount = Math.Min(
                maximumValuesPerRequest, totalValues - completedValues);
            byte[] payload = new byte[valueCount * 13];
            for (int index = 0; index < valueCount; index++)
            {
                int payloadOffset = index * 13;
                int sourceOffset = (completedValues + index) * sizeof(ulong);
                payload[payloadOffset] = (byte)PineOpcode.Write64;
                uint valueAddress = checked(address + (uint)sourceOffset);
                BinaryPrimitives.WriteUInt32LittleEndian(
                    payload.AsSpan(payloadOffset + 1, sizeof(uint)), valueAddress);
                data.Slice(sourceOffset, sizeof(ulong)).CopyTo(
                    payload.AsSpan(payloadOffset + 5, sizeof(ulong)));
            }

            byte[] response = Request(payload);
            if (response.Length != 0)
            {
                throw new InvalidDataException(
                    $"PINE block write returned {response.Length} unexpected bytes.");
            }

            completedValues += valueCount;
        }
    }

    public byte[] Request(ReadOnlySpan<byte> payload)
    {
        ObjectDisposedException.ThrowIf(disposed, this);
        if (payload.Length == 0)
        {
            throw new ArgumentException("A PINE request cannot be empty.", nameof(payload));
        }

        lock (requestLock)
        {
            byte[] request = new byte[payload.Length + sizeof(uint)];
            BinaryPrimitives.WriteUInt32LittleEndian(
                request.AsSpan(0, sizeof(uint)), checked((uint)request.Length));
            payload.CopyTo(request.AsSpan(sizeof(uint)));
            stream.Write(request);

            Span<byte> sizeBuffer = stackalloc byte[sizeof(uint)];
            stream.ReadExactly(sizeBuffer);
            uint responseSize = BinaryPrimitives.ReadUInt32LittleEndian(sizeBuffer);
            if (responseSize < 5 || responseSize > MaximumResponseBytes)
            {
                throw new InvalidDataException(
                    $"PINE returned an invalid response size: {responseSize}.");
            }

            byte[] framedResponse = new byte[checked((int)responseSize - sizeof(uint))];
            stream.ReadExactly(framedResponse);
            if (framedResponse[0] != 0)
            {
                throw new InvalidOperationException("PCSX2 rejected the PINE request.");
            }

            return framedResponse[1..];
        }
    }

    public void Dispose()
    {
        if (disposed)
        {
            return;
        }

        disposed = true;
        stream.Dispose();
        tcpClient.Dispose();
    }

    private ulong ReadInteger(uint address, int bits)
    {
        PineOpcode opcode = bits switch
        {
            8 => PineOpcode.Read8,
            16 => PineOpcode.Read16,
            32 => PineOpcode.Read32,
            64 => PineOpcode.Read64,
            _ => throw new ArgumentOutOfRangeException(nameof(bits)),
        };

        Span<byte> payload = stackalloc byte[5];
        payload[0] = (byte)opcode;
        BinaryPrimitives.WriteUInt32LittleEndian(payload[1..], address);
        byte[] response = Request(payload);
        int expectedSize = bits / 8;
        if (response.Length != expectedSize)
        {
            throw new InvalidDataException(
                $"PINE {bits}-bit read returned {response.Length} bytes.");
        }

        return bits switch
        {
            8 => response[0],
            16 => BinaryPrimitives.ReadUInt16LittleEndian(response),
            32 => BinaryPrimitives.ReadUInt32LittleEndian(response),
            64 => BinaryPrimitives.ReadUInt64LittleEndian(response),
            _ => throw new UnreachableException(),
        };
    }

    private void WriteInteger(uint address, ulong value, int bits)
    {
        PineOpcode opcode = bits switch
        {
            8 => PineOpcode.Write8,
            16 => PineOpcode.Write16,
            32 => PineOpcode.Write32,
            64 => PineOpcode.Write64,
            _ => throw new ArgumentOutOfRangeException(nameof(bits)),
        };

        int valueSize = bits / 8;
        Span<byte> payload = stackalloc byte[5 + sizeof(ulong)];
        payload[0] = (byte)opcode;
        BinaryPrimitives.WriteUInt32LittleEndian(payload[1..5], address);
        switch (bits)
        {
            case 8:
                payload[5] = checked((byte)value);
                break;
            case 16:
                BinaryPrimitives.WriteUInt16LittleEndian(
                    payload[5..], checked((ushort)value));
                break;
            case 32:
                BinaryPrimitives.WriteUInt32LittleEndian(
                    payload[5..], checked((uint)value));
                break;
            case 64:
                BinaryPrimitives.WriteUInt64LittleEndian(payload[5..], value);
                break;
        }

        byte[] response = Request(payload[..(5 + valueSize)]);
        if (response.Length != 0)
        {
            throw new InvalidDataException(
                $"PINE {bits}-bit write returned {response.Length} unexpected bytes.");
        }
    }
}
