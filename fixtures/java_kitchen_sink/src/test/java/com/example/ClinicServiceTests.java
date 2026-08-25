package com.example;

import static org.mockito.Mockito.when;

import com.example.repo.OwnerRepository;
import com.example.service.ClinicServiceImpl;
import org.junit.jupiter.api.Test;
import org.mockito.InjectMocks;
import org.mockito.Mock;

public class ClinicServiceTests {

    @Mock
    private OwnerRepository ownerRepository;

    @InjectMocks
    private ClinicServiceImpl clinicService;

    @Test
    void findOwnersByLastName() {
        when(ownerRepository.findByLastName("%")).thenReturn("owner");
        clinicService.findOwners();
    }
}
