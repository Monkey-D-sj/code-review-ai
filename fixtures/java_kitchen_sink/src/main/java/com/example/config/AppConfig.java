package com.example.config;

import com.example.repo.OwnerRepository;
import com.example.repo.OwnerRepositoryImpl;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.ComponentScan;
import org.springframework.context.annotation.Configuration;

@Configuration
@ComponentScan("com.example")
public class AppConfig {

    @Bean
    public OwnerRepository ownerRepository() {
        return new OwnerRepositoryImpl();
    }
}
