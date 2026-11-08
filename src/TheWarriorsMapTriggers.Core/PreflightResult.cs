// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 mostdak1ng

using System.Numerics;

namespace TheWarriorsMapTriggers;

public sealed record PreflightResult(
    GameIdentity Identity,
    Vector3 CameraPosition,
    Vector3 PlayerPosition,
    bool RecoverableOrphanHook,
    bool WorldReady = true)
{
    public static PreflightResult Run(int port = BuildInfo.DefaultPinePort)
    {
        using PineClient client = PineClient.Connect(port);
        GameIdentity identity = GameIdentity.Read(client);
        identity.EnsureSupported();

        ValidateInstruction(
            client,
            GameAddresses.MainViewHook,
            GameAddresses.MainViewHookOriginal,
            "MapTriggers/native renderer hook");
        ValidateInstruction(
            client,
            GameAddresses.CameraBeginUpdateHook,
            GameAddresses.CameraBeginUpdateOriginal,
            "camera override hook");
        ValidateInstruction(
            client,
            GameAddresses.BootstrapHook,
            GameAddresses.BootstrapHookOriginal,
            "heap bootstrap hook");

        uint padHook = client.Read32(GameAddresses.PadRefreshCall);
        byte[] cave = client.ReadBlock(
            GameAddresses.AllocatorBootstrapScratch, GameAddresses.AllocatorBootstrapScratchSize);
        if (padHook != GameAddresses.PadRefreshOriginal || cave.Any(static value => value != 0))
            throw new InvalidOperationException("The pad hook or allocator scratch is occupied. Close other tools and restart the game.");
        ValidateInstruction(client, 0x1989B0, 0x27BDFFB0, "native camera setter");
        ValidateInstruction(client, 0x1989B4, 0x00A0102D, "native camera setter delay slot");

        try
        {
            var camera = new CameraController(client);
            Vector3 player = PlayerController.ReadPlayerPosition(client);
            return new PreflightResult(identity, camera.Position, player, false);
        }
        catch (WorldUnavailableException)
        {
            // Menus/Game Over still support O and safe stop while the backend
            // waits for the same allocator-ready world as the primary launcher.
            return new PreflightResult(identity, Vector3.Zero, Vector3.Zero, false, false);
        }
    }

    private static void ValidateInstruction(
        PineClient client,
        uint address,
        uint expected,
        string label)
    {
        uint current = client.Read32(address);
        ValidateInstructionValue(current, expected, address, label);
    }

    public static void ValidateInstructionValue(uint current, uint expected, uint address, string label)
    {
        if (current != expected)
        {
            throw new InvalidOperationException(
                $"{label} is occupied at 0x{address:X8}: 0x{current:X8}. " +
                "Close other PINE tools and restart the game.");
        }
    }
}
