package com.acme.p0.inheritance;

sealed class SealedBase permits SealedChild {}

final class SealedChild extends SealedBase {}

sealed interface SealedContract permits SealedImpl {}

final class SealedImpl implements SealedContract {}
