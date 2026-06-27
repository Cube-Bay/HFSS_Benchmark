# HFSS Benchmark

这是一个用于测试 **Ansys HFSS / AEDT 仿真性能** 的小工具。

程序会自动在 HFSS 中建立一个固定的滤波器模型，然后运行仿真并给出跑分结果。

主要用途是比较不同电脑、CPU、内存配置、HFSS 版本下的仿真速度。

做这个项目的初衷是我目前长时间跑HFSS的设备是2679-V4，也就是垃圾佬喜闻乐见的“E5洋垃圾”（话虽这么说，但是79V4也是当年顶配了），最近我看中了EPYC的7F52/72，但是不清楚他是否比79V4强，如果强的话，强多少。

我上网找遍了有关的benchmark，发现没有人做过这个，于是本项目应运而生。

**本项目的代码完全由AI生成，主打一个“能跑就行”**

## 这个程序能做什么？

- 自动打开 HFSS / AEDT
- 自动建立一个固定的 7 阶 interdigital 滤波器模型（这里使用了Filter Solution导出的尺寸txt文件辅助，直接让AI建模还是太困难了）
- 自动设置仿真 Setup 和 Sweep
- 自动运行跑分
- 显示频点吞吐量柱状图
- 支持多轮跑分，并显示每一轮的折线图
- 支持导出和导入跑分结果，方便不同机器之间对比
- 支持打包成 exe 使用


## 运行环境

需要：

- Windows
- Ansys Electronics Desktop / HFSS
- Python 3.10 或更新版本
- 有可用的 HFSS license（~~懂的都懂~~）

安装 Python 依赖：

```powershell
python -m pip install -r requirements.txt
```

## 运行

把主程序和图片放在同一个文件夹，例如：

```text
HFSS_Benchmark/
├─ hfss_benchmark.py
├─ photo.jpg
└─ requirements.txt
```

然后运行：

```powershell
python hfss_benchmark.py
```

## 图片文件

程序会自动读取模型介绍图片。

推荐图片名：

```text
photo.jpg
```

也支持：

```text
photo.jpeg
photo.png
```

源码运行时，把图片放在主程序同目录即可。

## 打包成 exe

可以使用 `auto-py-to-exe`。

安装：

```powershell
python -m pip install auto-py-to-exe
```

打开：

```powershell
auto-py-to-exe
```

推荐设置：

```text
单文件：单文件
控制台窗口：基于窗口的隐藏控制台
```

如果想把图片一起打包进 exe，在“附加文件”里添加：

```text
Source:
photo.jpg

Destination:
.
```

这样生成 exe 后，就不需要再把 `photo.jpg` 放在 exe 旁边。


## 跑分结果怎么看？

程序主要看这个指标：

```text
频点吞吐量 points/day
```

它表示一天大约能完成多少个扫频点。

数值越高，说明 HFSS 跑这个模型越快。

程序中：

```text
Tasks=1
```

表示单任务基准测试，也就是一个频点一个频点地跑。

```text
满载 Tasks
```

表示按照当前机器逻辑线程数进行满载测试，相较于单任务跑法，这种跑法跑相同频点数目的时候在内存中搬移数据的频率会大大降低

如果勾选连续测试，结果表会显示平均值，折线图会显示每一轮的实际分数。

## 导出和导入结果

跑完之后可以点击：

```text
导出跑分结果
```

会生成一个 JSON 文件。

其他机器跑完后也可以导出 JSON，然后在程序中点击：

```text
导入对比结果
```

这样就可以在柱状图里面对比多台机器的结果。

## 局限性

众所周知，HFSS某种意义上只是AEDT的一个工具，AEDT里面的求解器其实非常丰富，因为我个人跑HFSS比较多，所以主要针对HFSS去开发测试脚本；

不同的模型在不同的机器上有不同的结果，其实很难去选择一个完美的标准来衡量性能，所以我们也不用过于看重这个结果，我选择这个规模的模型主要是考虑到能让个人的笔记本或者一般PC也可以运行；

其实HFSS求解是要先经过自适应网格收敛的过程，然后才是正式的求解，这个脚本没有算自适应收敛的时间，想看自适应收敛能力的话，可以参考单Tasks的分数。

因为我个人使用的是23R2版本的HFSS，所以在这个版本上适配肯定是最好的，其他版本有一定概率跑不起来。