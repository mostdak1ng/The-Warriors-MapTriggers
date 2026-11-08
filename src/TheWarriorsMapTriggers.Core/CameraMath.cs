// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 mostdak1ng

using System.Numerics;

namespace TheWarriorsMapTriggers;

public static class CameraMath
{
    public static Quaternion NormalizeQuaternion(Quaternion quaternion)
    {
        float lengthSquared = quaternion.LengthSquared();
        if (!float.IsFinite(lengthSquared) || lengthSquared < 1.0e-12f)
        {
            throw new InvalidDataException("Camera quaternion is not finite and non-zero.");
        }

        return Quaternion.Normalize(quaternion);
    }

    public static Quaternion AxisAngle(Vector3 axis, float angle) =>
        Quaternion.CreateFromAxisAngle(Vector3.Normalize(axis), angle);

    public static Quaternion Multiply(Quaternion first, Quaternion second) =>
        NormalizeQuaternion(Quaternion.Multiply(first, second));

    public static Vector3 Rotate(Quaternion quaternion, Vector3 vector)
    {
        float x = quaternion.X;
        float y = quaternion.Y;
        float z = quaternion.Z;
        float w = quaternion.W;
        float tx = 2f * ((y * vector.Z) - (z * vector.Y));
        float ty = 2f * ((z * vector.X) - (x * vector.Z));
        float tz = 2f * ((x * vector.Y) - (y * vector.X));
        return new Vector3(
            vector.X + (w * tx) + (y * tz) - (z * ty),
            vector.Y + (w * ty) + (z * tx) - (x * tz),
            vector.Z + (w * tz) + (x * ty) - (y * tx));
    }

    public static Vector2 ApplyRadialDeadzone(Vector2 value, float deadzone)
    {
        if (deadzone is < 0f or >= 1f)
        {
            throw new ArgumentOutOfRangeException(nameof(deadzone));
        }

        float length = value.Length();
        if (!float.IsFinite(length) || length <= deadzone)
        {
            return Vector2.Zero;
        }

        float scaledLength = Math.Clamp(
            (length - deadzone) / (1f - deadzone), 0f, 1f);
        return value / length * scaledLength;
    }

    public static bool IsFinite(Vector3 value) =>
        float.IsFinite(value.X) &&
        float.IsFinite(value.Y) &&
        float.IsFinite(value.Z);
}
