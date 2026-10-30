// af_agent_c: 板端 AF 代理 C 版。零子进程 spawn, 单命令 ~130ms(settle 占 120ms)
// 协议: P<dac>=移动+测量  S=读统计  V<dac>=只移动  每行一命令
// 编译: <arm-gcc> -O2 -static -o af_agent_c af_agent.c
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/ioctl.h>
#include <sys/select.h>
#include <linux/i2c-dev.h>

#define I2C_DEV "/dev/i2c-4"
#define VCM_ADDR 0x0c
#define STATS_PATH "/sys/kernel/debug/rkisp_af/stats"

static int i2c_fd = -1;

static int vcm_set(int dac) {
    if (i2c_fd < 0) {
        i2c_fd = open(I2C_DEV, O_RDWR);
        if (i2c_fd < 0) return -1;
        if (ioctl(i2c_fd, I2C_SLAVE, VCM_ADDR) < 0) return -1;
    }
    unsigned char buf[3];
    buf[0] = 0x02; buf[1] = 0x00;          // 退出 power-down
    if (write(i2c_fd, buf, 2) != 2) return -1;
    buf[0] = 0x03;
    buf[1] = (unsigned char)((dac >> 8) & 3);
    buf[2] = (unsigned char)(dac & 255);
    if (write(i2c_fd, buf, 3) != 3) return -1;  // DAC 连续写, 大端 10bit
    return 0;
}

static double read_stats(void) {
    FILE *fp = fopen(STATS_PATH, "r");
    if (!fp) return 0;
    char line[256];
    double s = 0;
    int i = -2;
    // 中心加权: 15x15 网格, 中央7x7(切比雪夫距离<=3)1.0, 4-5 → 0.4, 外圈 0.1
    while (fgets(line, sizeof(line), fp)) {
        i++;
        if (i < 0) continue;                    // time= / sum_b= 两行
        int gr = i / 15, gc = i % 15;
        int dr = gr - 7; if (dr < 0) dr = -dr;
        int dc = gc - 7; if (dc < 0) dc = -dc;
        int dist = dr > dc ? dr : dc;
        double w = dist <= 2 ? 1.0 : (dist <= 4 ? 0.2 : 0.02);
        unsigned long v1, v2, h1, h2;
        if (sscanf(line, "%lu %lu %lu %lu", &v1, &v2, &h1, &h2) == 4)
            s += w * ((double)v1 + v2 + h1 + h2);
    }
    fclose(fp);
    return s;
}

int main(void) {
    char line[64];
    setvbuf(stdout, NULL, _IOLBF, 0);   // 行缓冲: 每次应答立即刷出
    while (1) {
        // stdin 5s 无数据 → exit: ssh 半死时自杀, 守护会重建新实例
        fd_set rfds;
        struct timeval tv = {5, 0};
        FD_ZERO(&rfds);
        FD_SET(0, &rfds);
        if (select(1, &rfds, NULL, NULL, &tv) <= 0) return 0;
        if (!fgets(line, sizeof(line), stdin)) return 0;
        size_t len = strlen(line);
        while (len && (line[len-1] == '\n' || line[len-1] == '\r')) line[--len] = 0;
        if (!len) continue;
        char cmd = line[0];
        int dac = atoi(line + 1);
        if (cmd == 'P') {
            if (vcm_set(dac) != 0) { printf("ERR\n"); continue; }
            const char *su = getenv("AF_SETTLE_US");
            usleep(su ? atoi(su) : 40000);   // settle: 环境变量可调, 默认 40ms
            printf("A%.0f\n", read_stats());
        } else if (cmd == 'V') {
            vcm_set(dac);
            printf("OK\n");
        } else if (cmd == 'S') {
            printf("A%.0f\n", read_stats());
        } else {
            printf("ERR\n");
        }
    }
    return 0;
}
