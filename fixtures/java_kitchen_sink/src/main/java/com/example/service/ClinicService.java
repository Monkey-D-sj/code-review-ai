package com.example.service;

public interface ClinicService {

    String findOwners();

    String findOwner(int ownerId);

    default String welcome() {
        return "welcome";
    }
}
