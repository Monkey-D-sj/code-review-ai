package com.example.repo;

import org.springframework.stereotype.Repository;

@Repository
public class OwnerRepositoryImpl implements OwnerRepository {

    @Override
    public String findByLastName(String lastName) {
        return "owner";
    }
}
