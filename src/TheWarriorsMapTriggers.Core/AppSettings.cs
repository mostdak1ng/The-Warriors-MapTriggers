// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 mostdak1ng
using System.Text.Json;

namespace TheWarriorsMapTriggers;

public sealed record AppSettings
{
    public string IsoPath { get; init; } = "";
    public int PinePort { get; init; } = BuildInfo.DefaultPinePort;
    public string Profile { get; init; } = "maptriggers";
    public bool CaptureKeyboardPad { get; init; }
    public bool ReturnToFollowCamera { get; init; }
    public bool StartWorldPaused { get; init; }
    public bool ExtendedCollision { get; init; }

    public static string DataDirectory => Environment.GetEnvironmentVariable("TW_MAPTRIGGERS_DATA")
        ?? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "TheWarriorsMapTriggers");

    public void Validate()
    {
        if (PinePort is < 1 or > 65535) throw new InvalidDataException("PINE port must be between 1 and 65535.");
        if (Profile is not ("freecam" or "maptriggers")) throw new InvalidDataException("Unknown start profile.");
    }

    public static AppSettings Load(string? directory = null)
    {
        string path = Path.Combine(directory ?? DataDirectory, "settings.json");
        if (!File.Exists(path)) return new();
        AppSettings settings = JsonSerializer.Deserialize<AppSettings>(File.ReadAllText(path))
            ?? throw new InvalidDataException("The settings file is empty.");
        settings.Validate();
        return settings;
    }

    public void Save(string? directory = null)
    {
        Validate();
        string root = directory ?? DataDirectory;
        Directory.CreateDirectory(root);
        string target = Path.Combine(root, "settings.json");
        string temporary = target + ".tmp";
        File.WriteAllText(temporary, JsonSerializer.Serialize(this, new JsonSerializerOptions { WriteIndented = true }));
        File.Move(temporary, target, overwrite: true);
    }
}
