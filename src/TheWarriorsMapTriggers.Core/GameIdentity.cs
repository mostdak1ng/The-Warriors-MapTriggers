// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 mostdak1ng

namespace TheWarriorsMapTriggers;

public sealed record GameIdentity(
    string EmulatorVersion,
    string Title,
    string Serial,
    string GameVersion,
    string Crc,
    PineStatus Status)
{
    public bool IsSupported =>
        Serial.Equals(BuildInfo.SupportedSerial, StringComparison.OrdinalIgnoreCase) &&
        GameVersion.Equals(
            BuildInfo.SupportedGameVersion, StringComparison.OrdinalIgnoreCase) &&
        Crc.Equals(BuildInfo.SupportedCrc, StringComparison.OrdinalIgnoreCase);

    public static GameIdentity Read(PineClient client) => new(
        client.ReadText(PineOpcode.Version),
        client.ReadText(PineOpcode.Title),
        client.ReadText(PineOpcode.GameId),
        client.ReadText(PineOpcode.GameVersion),
        client.ReadText(PineOpcode.Uuid).ToLowerInvariant(),
        client.ReadStatus());

    public void EnsureSupported()
    {
        if (!IsSupported)
        {
            throw new NotSupportedException(
                "Unsupported game executable. Expected " +
                $"{BuildInfo.SupportedSerial} v{BuildInfo.SupportedGameVersion} " +
                $"CRC {BuildInfo.SupportedCrc.ToUpperInvariant()}, received " +
                $"{Serial} v{GameVersion} CRC {Crc.ToUpperInvariant()}.");
        }
    }
}
