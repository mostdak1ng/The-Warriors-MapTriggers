// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 mostdak1ng

namespace TheWarriorsMapTriggers;

public sealed class WorldUnavailableException : Exception
{
    public WorldUnavailableException(string message)
        : base(message)
    {
    }

    public WorldUnavailableException(string message, Exception innerException)
        : base(message, innerException)
    {
    }
}
