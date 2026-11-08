// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 mostdak1ng

namespace TheWarriorsMapTriggers;

public static class BuildInfo
{
    public const string ProductName = "The Warriors MapTriggers";
    public static string Version => typeof(BuildInfo).Assembly.GetName().Version!.ToString(3);
    public const string Copyright = "Copyright (C) 2026 mostdak1ng";
    public const string License = "GPL-3.0-only";

    public const string SupportedSerial = "SLUS-21215";
    public const string SupportedGameVersion = "1.03";
    public const string SupportedCrc = "b99a75de";
    public const int DefaultPinePort = 28011;
}
