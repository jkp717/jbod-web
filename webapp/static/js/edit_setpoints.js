const queryString = window.location.search;
const urlParams = new URLSearchParams(queryString);
const fanId = urlParams.get('id');
let dataObj;
let chartObj;

$(function() {
    loadSetpointData(renderChart);
});

function loadSetpointData(callback) {
    var xhttp = new XMLHttpRequest();
    xhttp.onreadystatechange = function() {
        if (this.readyState == 4 && this.status == 200) {
            callback(JSON.parse(this.responseText));
        }
    }
    xhttp.open("GET", "/fan/setpoints?fan_id=" + fanId, true);
    xhttp.send();
}

function renderChart(responseData) {
    dataObj = responseData;
    var options = {
      type: 'bar',
      data: {
        labels: responseData.temp,
        datasets: [
          {
            label: "PWM",
            // backgroundColor: ["#3e95cd", "#8e5ea2","#3cba9f","#e8c3b9","#c45850"],
            data: responseData.pwm
          }
        ]
      },
      options: {
        responsive: false,
        onHover: function(e) {
          const point = e.chart.getElementsAtEventForMode(e, 'nearest', { intersect: true }, false)
          if (point.length) e.native.target.style.cursor = 'grab'
          else e.native.target.style.cursor = 'default'
        },
        plugins: {
          dragData: {
            round: 0,
            showTooltip: true,
            onDragStart: function(e) {
                // do nothing
            },
            onDrag: function(e, datasetIndex, index, value) {
              e.target.style.cursor = 'grabbing';
              // stop drag relative to position on chart
              if (index > 0 && dataObj.pwm[index-1] >= value) {
                return false;
              } else if (index < dataObj.temp.length && dataObj.pwm[index+1] <= value) {
                return false;
              } else {
                dataObj.pwm[index] = value;
              }
            },
            onDragEnd: function(e, datasetIndex, index, value) {
              e.target.style.cursor = 'default';
            },
          }
        },
        scales: {
          y: {
            max: 100,
            min: 0
          }
        }
      }
    }

    var ctx = document.getElementById('canvas').getContext('2d');
    chartObj = new Chart(ctx, options);
}

function buildModalForm(event, url) {
    if ($('#setpoint-modal').length) {
        $('#setpoint-modal').modal('show');
        return;
    }
    var xhttp = new XMLHttpRequest();
    xhttp.onreadystatechange = function() {
        if (this.readyState == 4 && this.status == 200) {
            document.getElementById("setpoint-modal-container").innerHTML= this.response;
            $('#setpoint-modal').modal('show');
            newSetpointHandler();
        }
    }
    xhttp.open("GET", url, true);
    xhttp.send();
}

function newSetpointHandler() {
  const form = document.getElementById("setpoint-modal").querySelector("form.admin-form");
  let newPWM;

  // Add 'submit' event handler
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const fd = new FormData(form);
    const XHR = new XMLHttpRequest();

    XHR.addEventListener("load", (event) => {
      $('#setpoint-modal').modal('hide');
      form.reset();
      loadSetpointData(updateChart);
    });

    XHR.addEventListener("error", (event) => {
      alert('Oops! Something went wrong.');
    });

    XHR.open("POST", "/setpoints/new?fan_id=" + fanId);
    XHR.send(fd);
  });
};

function updateChart(responseData) {
  dataObj = responseData;
  chartObj.data.labels = responseData.temp;
  chartObj.data.datasets[0].data = responseData.pwm;
  chartObj.update();
}


document.querySelector("form.admin-form").addEventListener("submit", (event) => {
    event.preventDefault();
        const XHR = new XMLHttpRequest();
    var postArray = [];
    for (let i = 0; i < chartObj.data.labels.length; i++) {
        postArray[i] = {
            'fan_id': fanId,
            'temp': chartObj.data.labels[i],
            'pwm': chartObj.data.datasets[0].data[i]
        };
    }
    XHR.addEventListener("load", (event) => {
        document.querySelector("form.admin-form").submit();
    });
    XHR.addEventListener("error", (event) => {
      alert('Oops! Something went wrong.');
    });
    XHR.open("POST", "/fan/setpoints?fan_id=" + fanId);
    XHR.setRequestHeader("Content-Type", 'application/json');
    XHR.send(JSON.stringify(postArray));
});


function buildModalFromURL(containerId, url, callback) {
    if ($("#" + containerId).find('.modal').length) {
        $("#" + containerId).find('.modal').modal('show');
        return;
    }
    var xhttp = new XMLHttpRequest();
    xhttp.onreadystatechange = function() {
        if (this.readyState == 4 && this.status == 200) {
            document.getElementById(containerId).innerHTML= this.response;
            $("#" + containerId).find('.modal').modal('show');
            if (callback) callback();
        }
    }
    xhttp.open("GET", url, true);
    xhttp.send();
}

function deleteSetpoint(url) {
    var spId = document.getElementById('setpoint-delete-select').value;
        var xhttp = new XMLHttpRequest();
    xhttp.onreadystatechange = function() {
        if (this.readyState == 4 && this.status == 200) {
            $('#setpoint-delete-modal').modal('hide');
            loadSetpointData(updateChart);
        }
    }
    xhttp.open("POST", url, true);
    xhttp.send(JSON.stringify({'id': spId}));
}