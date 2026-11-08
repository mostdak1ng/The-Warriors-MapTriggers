// SPDX-License-Identifier: GPL-3.0-only
namespace TheWarriorsMapTriggers;

public static class SessionStopSignal
{
    public static void Request(string directory)
    {
        string stop = Path.Combine(directory, "stop.flag");
        if (File.Exists(stop) || File.Exists(Path.Combine(directory, "cleanup_wait.flag")))
            File.WriteAllText(Path.Combine(directory, "exit_now.flag"), "stop");
        File.WriteAllText(stop, "stop");
    }
}
