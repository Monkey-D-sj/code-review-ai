package com.example.util;

public final class CommonUtil {

    private CommonUtil() {
    }

    public static String trim(String value) {
        return value == null ? "" : value.trim();
    }

    public static String upper(String value) {
        return value == null ? "" : value.toUpperCase();
    }
}
