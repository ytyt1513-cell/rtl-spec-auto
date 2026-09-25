// Regression sources. Module names deliberately differ from the file name.
module legacy_latch(clk, rst_n, put, clear, payload, value, valid);
parameter W = 8;
input clk, rst_n;
input put, clear;
input [W-1:0] payload;
output [W-1:0] value;
output valid;
reg [W-1:0] value;
reg valid;
always @(posedge clk or negedge rst_n)
  if (!rst_n) begin value <= 0; valid <= 0; end
  else begin
    if (clear) valid <= 0;
    if (put) begin value <= payload; valid <= 1; end
  end
endmodule

module compact_ctrl(input clk, input rst_n, input start, cancel, input [7:0] din, output reg [7:0] dout, output reg busy, done);
always @(posedge clk or negedge rst_n)
  if (!rst_n) begin dout<=0; busy<=0; done<=0; end
  else begin
    done<=0;
    if (cancel) busy<=0;
    else if (busy) begin busy<=0; done<=1; end
    else if (start) begin dout<=din; busy<=1; end
  end
endmodule

module bus_shell(input logic clk, reset_n, wr, ack,
                 input logic [7:0] din,
                 output logic [7:0] dout,
                 output logic pending);
  legacy_latch #(.W(8)) u_hold(clk, reset_n, wr, ack, din, dout, pending);
  watch_flag monitor(.clk(clk), .rst_n(reset_n), .valid(pending));
endmodule

module watch_flag(input clk, rst_n, valid);
  reg observed;
  always @(posedge clk or negedge rst_n)
    if (!rst_n) observed <= 0;
    else observed <= valid;
endmodule
