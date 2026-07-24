#include <stdlib.h>
#include <stdio.h>
#include <string.h>
int main() {
    size_t total = 0;
    for (int i = 0; i < 20; i++) {
        size_t chunk = 64UL << 20;
        void *p = malloc(chunk);
        if (!p) break;
        memset(p, 0xff, chunk);
        total += chunk;
        fprintf(stderr, "allocated %zu MB\n", total >> 20);
    }
    printf("SURVIVED_ALL %zu MB\n", total >> 20);
    return 0;
}
