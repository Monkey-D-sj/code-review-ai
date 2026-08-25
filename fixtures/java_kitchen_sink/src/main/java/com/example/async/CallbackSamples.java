package com.example.async;

import java.util.List;
import java.util.Optional;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.stream.Collectors;

public class CallbackSamples {

    private final ExecutorService pool = Executors.newFixedThreadPool(4);

    public List<String> mapNames(List<String> names) {
        return names.stream()
                .map(String::toUpperCase)
                .collect(Collectors.toList());
    }

    public void runAsync(Runnable task) {
        pool.submit(task);
    }

    public CompletableFuture<String> loadAsync(String key) {
        return CompletableFuture.supplyAsync(() -> fetch(key));
    }

    public Optional<String> firstNonBlank(List<String> names) {
        return names.stream()
                .filter(name -> name != null && !name.isBlank())
                .findFirst();
    }

    private String fetch(String key) {
        return key;
    }
}
