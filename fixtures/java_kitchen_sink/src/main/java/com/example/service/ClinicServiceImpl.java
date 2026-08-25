package com.example.service;

import com.example.repo.OwnerRepository;
import org.springframework.stereotype.Service;

@Service
public class ClinicServiceImpl implements ClinicService {

    private final OwnerRepository ownerRepository;

    public ClinicServiceImpl(OwnerRepository ownerRepository) {
        this.ownerRepository = ownerRepository;
    }

    @Override
    public String findOwners() {
        return ownerRepository.findByLastName("%");
    }

    @Override
    public String findOwner(int ownerId) {
        return ownerRepository.findByLastName(String.valueOf(ownerId));
    }
}
