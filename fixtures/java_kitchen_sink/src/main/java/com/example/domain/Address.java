package com.example.domain;

public record Address(String city, String street) {

    public Address {
        if (city == null || city.isBlank()) {
            throw new IllegalArgumentException("city must not be blank");
        }
    }

    public String full() {
        return city + " " + street;
    }
}
