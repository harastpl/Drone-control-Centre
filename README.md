# Drone-control-Centre
Flask and pywebview based software


To Run, First create virtual env
``` python -m venv myenv```

Then install the libraries

``` pip install -r requirements.txt ```

then run main.py

if for some reason no window is opening try modifing test_pywebview.py or min_test.py and make appropirate changes in pywebview calls in main.py.

To run the code uploader a secondry sub process is run by stm library during modifying code please keep that in mind and handle it gracefully.
