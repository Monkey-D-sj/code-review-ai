package com.example.repo;

import com.example.domain.Owner;
import org.springframework.data.jpa.repository.JpaRepository;

public interface OwnerRepository extends JpaRepository<Owner, Integer> {

    String findByLastName(String lastName);
}
