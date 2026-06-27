# HFSS Benchmark

这是一个用于测试 **Ansys HFSS / AEDT 仿真性能** 的小工具。

程序会自动在 HFSS 中建立一个固定的滤波器模型，然后运行仿真并给出跑分结果。  
主要用途是比较不同电脑、CPU、内存配置、HFSS 版本下的仿真速度。

---

## 这个程序能做什么？

- 自动打开 HFSS / AEDT
- 自动建立一个固定的 7 阶 interdigital 滤波器模型
- 自动设置仿真 Setup 和 Sweep
- 自动运行跑分
- 显示频点吞吐量柱状图
- 支持多轮跑分，并显示每一轮的折线图
- 支持导出和导入跑分结果，方便不同机器之间对比
- 支持打包成 exe 使用

---

## 运行环境

需要：

- Windows
- Ansys Electronics Desktop / HFSS
- Python 3.10 或更新版本
- 有可用的 HFSS license

安装 Python 依赖：

```powershell
python -m pip install -r requirements.txt
```

---

## 直接运行

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

其中 `hfss_benchmark.py` 就是主程序源码文件。  
如果你没有改名，也可以直接运行原来的文件名。

---

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

---

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

---

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

表示单任务基准测试。

```text
满载 Tasks
```

表示按照当前机器逻辑线程数进行满载测试。

如果勾选连续测试，结果表会显示平均值，折线图会显示每一轮的实际分数。

---

## 导出和导入结果

跑完之后可以点击：

```text
导出跑分结果
```

会生成一个 JSON 文件。

其他机器跑完后也可以导出 JSON，然后在你的电脑上点击：

```text
导入对比结果
```

这样就可以在柱状图里面对比多台机器的结果。

---

## 常见问题

### 1. 程序找不到 HFSS

先确认已经安装 Ansys Electronics Desktop。  
如果装了多个版本，可以在界面里刷新或选择 HFSS 版本。

### 2. 图片不显示

检查图片名是否为：

```text
photo.jpg
photo.jpeg
photo.png
```

如果是 exe 版本，确认打包时已经把图片加入“附加文件”。

### 3. 打包后运行不了

先用源码方式运行一遍，确认 Python 环境和 HFSS 都没问题。  
打包时需要把依赖一起打进去，尤其是：

```text
pyaedt
pywin32
pillow
```

### 4. 跑分结果不稳定

HFSS 仿真会受后台任务、内存占用、CPU 温度、license 状态等影响。  
建议多跑几轮，看平均值和折线图。

---

## 建议的仓库结构

```text
HFSS_Benchmark/
├─ hfss_benchmark.py
├─ photo.jpg
├─ README.md
├─ requirements.txt
├─ .gitignore
└─ LICENSE
```

如果不想上传图片，也可以只上传代码和说明，让用户自己准备 `photo.jpg`。

---

## License

MIT License。
