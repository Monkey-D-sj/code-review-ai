package com.example.controller;

import com.example.service.ClinicService;
import org.springframework.stereotype.Controller;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;

@Controller
@RequestMapping("/owners")
public class OwnerController {

    private final ClinicService clinicService;

    public OwnerController(ClinicService clinicService) {
        this.clinicService = clinicService;
    }

    @GetMapping
    public String findOwners() {
        return clinicService.findOwners();
    }

    @GetMapping("/{ownerId}")
    public String showOwner(int ownerId) {
        return clinicService.findOwner(ownerId);
    }
}
