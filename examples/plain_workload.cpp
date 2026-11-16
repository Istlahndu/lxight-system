// No profiler SDK. Independent native threads perform ordinary CPU work.
#include <cmath>
#include <cstdio>
#include <thread>
#include <vector>
#include <chrono>
int main() {
    std::vector<double> result(3, 0.0);
    std::vector<std::thread> workers;
    for (int t=0; t<3; ++t) workers.emplace_back([t,&result] {
        double x=0;
        for (int b=0;b<36;++b) {
            for (int i=0;i<220000;++i) x+=std::sqrt(double((i+t)%1021+1));
            if (b%6==0) std::this_thread::sleep_for(std::chrono::milliseconds(8));
        }
        result[t]=x;
    });
    for (auto &t:workers) t.join();
    std::printf("checksum=%.6f\n",result[0]+result[1]+result[2]);
}
